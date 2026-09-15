# core/auth_routes.py
# ========== HTTP 层认证：登录/注册/登出/当前用户 + 全局认证与 CSRF 中间件 ==========
#
# 设计原则：
#   1. 会话凭证：secrets.token_urlsafe(32) 生成随机 token，放 HttpOnly Cookie
#      （codeagent_session），数据库只存 SHA-256 哈希 → 泄库也无法反推凭证
#   2. CSRF 防护：登录成功下发 csrf_token（响应体），前端 auth.js 通过
#      X-CSRF-Token 头回传；所有 /api/* 变更请求由中间件统一校验
#   3. 认证保护：中间件统一拦截 /api/*（login/register/health 除外），
#      未认证返回 401，前端 auth.js 收到 401 自动跳转 /login
#   4. 暴力破解：AuthRateLimiter 按 IP 滑动窗口限流登录/注册
#   5. 归属校验：ensure_session_ownership 由各带 session_id 的端点调用，
#      首次访问声明归属，他人会话直接 403（防横向越权）
#
# 前端契约（static/auth.js）：
#   POST /api/auth/login    {username, password} → {user, csrf_token} + Set-Cookie
#   POST /api/auth/register {username, password} → {user, csrf_token} + Set-Cookie（注册即登录）
#   GET  /api/auth/me       → {user, csrf_token}（401 = 未登录）
#   POST /api/auth/logout   （需 X-CSRF-Token 头）→ 清除 Cookie

import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from config import (
    AUTH_SESSION_TTL_SECONDS,
    AUTH_COOKIE_SECURE,
    AUTH_LOGIN_ATTEMPT_LIMIT,
    AUTH_LOGIN_WINDOW_SECONDS,
    AUTH_REGISTER_ATTEMPT_LIMIT,
    AUTH_REGISTER_WINDOW_SECONDS,
)
from core.auth_security import (
    AUTH_COOKIE_NAME,
    CSRF_HEADER_NAME,
    AuthRateLimiter,
    CredentialValidationError,
    generate_csrf_token,
    generate_session_token,
    hash_password,
    hash_session_token,
    validate_password,
    validate_username,
    verify_password,
)
from core.auth_store import (
    AuthSessionRecord,
    DuplicateUsernameError,
    SQLiteAuthStore,
    SessionOwnershipError,
    get_auth_store,
)

# ===================== 请求/响应模型 =====================

class AuthCredentialsRequest(BaseModel):
    """登录/注册共用的请求体。"""
    username: str
    password: str


# ===================== 限流器（进程级单例） =====================

_login_limiter = AuthRateLimiter()
_register_limiter = AuthRateLimiter()


def _client_key(request: Request, prefix: str) -> str:
    """按客户端 IP 构造限流 key（uvicorn 单机部署，无代理场景直接取对端地址）。"""
    return f"{prefix}:{request.client.host if request.client else 'unknown'}"


# ===================== 会话解析与 Cookie =====================

def resolve_auth_session(request: Request) -> AuthSessionRecord | None:
    """从请求 Cookie 解析登录会话；无效/过期/已登出返回 None。"""
    token = request.cookies.get(AUTH_COOKIE_NAME)
    if not token:
        return None
    store = get_auth_store()
    return store.get_auth_session(hash_session_token(token))


def _set_auth_cookie(response: JSONResponse, token: str) -> None:
    """写入登录 Cookie：HttpOnly 防 JS 读取，SameSite=Lax 兼顾 CSRF 与导航。"""
    response.set_cookie(
        AUTH_COOKIE_NAME,
        token,
        max_age=AUTH_SESSION_TTL_SECONDS,
        httponly=True,
        secure=AUTH_COOKIE_SECURE,
        samesite="lax",
        path="/",
    )


def _clear_auth_cookie(response: JSONResponse) -> None:
    """登出时清除登录 Cookie。"""
    response.delete_cookie(AUTH_COOKIE_NAME, path="/")


def _session_payload(session: AuthSessionRecord) -> dict:
    """登录态的标准响应体（login/register/me 共用，前端读 user.username 和 csrf_token）。"""
    return {
        "user": {"id": session.user.id, "username": session.user.username},
        "csrf_token": session.csrf_token,
        "expires_at": session.expires_at,
    }


# ===================== 归属校验（供业务端点调用） =====================

async def ensure_session_ownership(
    auth_session: AuthSessionRecord,
    session_id: str,
    store: SQLiteAuthStore | None = None,
) -> None:
    """校验聊天会话归属：首次访问声明归属，他人会话抛 403。

    所有带 session_id 的业务端点在处理前调用本函数，
    从根上封堵「猜 session_id → 读/删他人会话」的横向越权。
    """
    s = store or get_auth_store()
    try:
        # create_chat_session 原子语义：不存在则创建（声明归属），
        # 已存在则校验 user_id 一致，不一致抛 SessionOwnershipError
        await run_in_threadpool(s.create_chat_session, auth_session.user.id, session_id)
    except SessionOwnershipError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


# ===================== 认证路由 =====================

router = APIRouter(prefix="/api/auth", tags=["认证"])


@router.post("/register")
async def register(req: AuthCredentialsRequest, request: Request):
    """注册新用户并直接进入登录态（注册成功即 Set-Cookie）。

    限流：同 IP 1 小时内最多 5 次注册。
    """
    # 1. 限流（超出窗口直接 429，不暴露用户名是否已存在）
    key = _client_key(request, "register")
    if not _register_limiter.consume(key, AUTH_REGISTER_ATTEMPT_LIMIT, AUTH_REGISTER_WINDOW_SECONDS):
        return JSONResponse(
            {"error": "注册尝试过于频繁，请 1 小时后再试"},
            status_code=429,
        )

    # 2. 凭证格式校验（3-32 位用户名 / 10-72 字节密码）
    try:
        display_name, normalized = validate_username(req.username)
        validate_password(req.password)
    except CredentialValidationError as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    # 3. 创建用户（bcrypt 哈希落库）
    store = get_auth_store()
    try:
        user = await run_in_threadpool(
            store.create_user,
            display_name,
            normalized,
            hash_password(req.password),
        )
    except DuplicateUsernameError:
        return JSONResponse({"error": "用户名已存在"}, status_code=409)

    # 4. 注册即登录：创建会话 + 下发 Cookie
    session, token = await _issue_session(store, user.id)
    _register_limiter.clear(key)
    response = JSONResponse(_session_payload(session))
    _set_auth_cookie(response, token)
    return response


@router.post("/login")
async def login(req: AuthCredentialsRequest, request: Request):
    """用户名密码登录，成功返回 csrf_token 并 Set-Cookie。

    限流：同 IP 15 分钟内最多 5 次尝试（成功后清零）。
    失败统一返回「用户名或密码错误」，不区分用户不存在/密码错误。
    """
    # 1. 限流
    key = _client_key(request, "login")
    if not _login_limiter.consume(key, AUTH_LOGIN_ATTEMPT_LIMIT, AUTH_LOGIN_WINDOW_SECONDS):
        return JSONResponse(
            {"error": "登录尝试过于频繁，请 15 分钟后再试"},
            status_code=429,
        )

    # 2. 格式校验（格式非法直接按认证失败处理，不暴露校验细节）
    try:
        _, normalized = validate_username(req.username)
    except CredentialValidationError:
        return JSONResponse({"error": "用户名或密码错误"}, status_code=401)
    try:
        validate_password(req.password)
    except CredentialValidationError:
        return JSONResponse({"error": "用户名或密码错误"}, status_code=401)

    # 3. 查用户 + bcrypt 验证（恒定失败语义，防用户枚举）
    store = get_auth_store()
    credentials = await run_in_threadpool(store.get_user_credentials, normalized)
    if credentials is None or not verify_password(req.password, credentials.password_hash):
        return JSONResponse({"error": "用户名或密码错误"}, status_code=401)
    if not credentials.is_active:
        return JSONResponse({"error": "账号已被禁用"}, status_code=403)

    # 4. 签发会话
    session, token = await _issue_session(store, credentials.id)
    _login_limiter.clear(key)
    response = JSONResponse(_session_payload(session))
    _set_auth_cookie(response, token)
    return response


@router.get("/me")
async def me(request: Request):
    """查询当前登录态（前端启动时调用，401 触发跳转 /login）。"""
    session = getattr(request.state, "auth_session", None)
    if session is None:
        raise HTTPException(status_code=401, detail="未登录")
    return _session_payload(session)


@router.post("/logout")
async def logout(request: Request):
    """登出：吊销服务端会话并清除 Cookie（中间件已校验 CSRF 头）。"""
    session = getattr(request.state, "auth_session", None)
    if session is None:
        raise HTTPException(status_code=401, detail="未登录")

    store = get_auth_store()
    await run_in_threadpool(store.revoke_auth_session, session.token_hash)
    response = JSONResponse({"status": "logged_out"})
    _clear_auth_cookie(response)
    return response


# ===================== 会话签发辅助 =====================

async def _issue_session(store: SQLiteAuthStore, user_id: str) -> tuple[AuthSessionRecord, str]:
    """生成新登录会话：返回 (会话记录, 明文 token)。

    明文 token 只用于写入 Cookie（HttpOnly），数据库只存其 SHA-256 哈希。
    """
    now = time.time()
    token = generate_session_token()
    csrf_token = generate_csrf_token()
    session = await run_in_threadpool(
        store.create_auth_session,
        user_id,
        token_hash=hash_session_token(token),
        csrf_token=csrf_token,
        expires_at=now + AUTH_SESSION_TTL_SECONDS,
        now=now,
    )
    return session, token


__all__ = [
    "router",
    "resolve_auth_session",
    "ensure_session_ownership",
]
