# tools/code_tool/path_security.py
# ========== 路径安全校验 + 白名单管理 ==========
#
# 职责：
#   1. 白名单管理（内置 + 静态 + 会话动态）
#   2. 黑名单二次校验（系统目录禁止）
#   3. 路径校验入口（_validate_path / _validate_write_path）
#
# 白名单层级：
#   1. 内置白名单：上传目录 + 快照目录（始终生效）
#   2. 静态白名单：config.ALLOWED_WORKSPACES（默认空）
#   3. 会话动态白名单：用户主动"打开项目"后加入

import re
from pathlib import Path

from config import (
    CODE_HISTORY_DIR, BLOCKED_DIRS, ALLOWED_WORKSPACES,
    UPLOAD_DIR,
    WORKSPACE_MAX_PROJECTS,
    WORKSPACE_ALLOWED_ROOTS,
)


# ------------------------------------------------------------------
# 静态白名单（启动时算一次）
# ------------------------------------------------------------------
def _normalize_workspaces() -> list[Path]:
    """把 ALLOWED_WORKSPACES 配置规范化为绝对 Path 列表（启动时算一次）。

    相对路径以 cwd（项目根 simple/）为基准解析。
    """
    return [Path(ws).resolve() for ws in ALLOWED_WORKSPACES]


# 模块级缓存：静态白名单绝对路径列表（避免每次校验都重新 resolve）
_ALLOWED_PATHS: list[Path] = _normalize_workspaces()


# ------------------------------------------------------------------
# 内置白名单：项目自身功能目录（无需用户打开项目即可访问）
# ------------------------------------------------------------------
# 上传文件目录（data/workspace/uploads/）和快照目录（data/workspace/history/）
# 是项目内置功能的一部分，无论用户是否打开项目都需要能访问
_BUILTIN_ALLOWED_PATHS: list[Path] = [
    Path(UPLOAD_DIR).resolve(),
    Path(CODE_HISTORY_DIR).resolve(),
]


# ------------------------------------------------------------------
# 会话级动态白名单（打开项目时动态扩展）
# ------------------------------------------------------------------
# 结构：{session_id: [Path(项目根1), Path(项目根2), ...]}
# 内存维护，进程重启清空（如需持久化可后续接入 SQLite）
_SESSION_WORKSPACES: dict[str, list[Path]] = {}


def add_session_workspace(session_id: str, project_path: str) -> Path:
    """打开项目时调用，把项目根路径加入会话级白名单。

    :param session_id: 会话 ID
    :param project_path: 项目根目录绝对路径
    :return: 规范化后的 Path 对象
    :raises ValueError: 路径不存在、不是目录、超限、命中黑名单或前缀不允许
    """
    if not session_id:
        raise ValueError("session_id 不能为空")

    p = Path(project_path).resolve()

    # 1. 必须存在且是目录
    if not p.exists():
        raise ValueError(f"项目目录不存在: {p}")
    if not p.is_dir():
        raise ValueError(f"路径不是目录: {p}")

    # 2. 黑名单校验（系统目录禁止）
    if _is_blocked(p):
        raise ValueError(f"禁止访问系统目录: {p}")

    # 3. 根目录前缀限制（可选，配置 WORKSPACE_ALLOWED_ROOTS 后生效）
    if WORKSPACE_ALLOWED_ROOTS:
        allowed_prefix = any(
            str(p).lower().startswith(root.lower())
            for root in WORKSPACE_ALLOWED_ROOTS
        )
        if not allowed_prefix:
            raise ValueError(
                f"项目路径不在允许的根目录前缀内: {p}（允许前缀: {WORKSPACE_ALLOWED_ROOTS}）"
            )

    # 4. 防止重复加入
    current = _SESSION_WORKSPACES.get(session_id, [])
    for existing in current:
        if existing == p:
            return p   # 已存在，幂等返回

    # 5. 数量限制
    if len(current) >= WORKSPACE_MAX_PROJECTS:
        raise ValueError(
            f"会话已打开 {len(current)} 个项目，达到上限 {WORKSPACE_MAX_PROJECTS}"
        )

    # 6. 加入会话白名单
    _SESSION_WORKSPACES.setdefault(session_id, []).append(p)
    return p


def remove_session_workspace(session_id: str, project_path: str = None) -> int:
    """关闭项目时调用，移除会话级白名单。

    :param session_id: 会话 ID
    :param project_path: 指定关闭的项目路径；为空时关闭该会话所有项目
    :return: 移除的项目数
    """
    if session_id not in _SESSION_WORKSPACES:
        return 0

    if project_path is None:
        # 关闭该会话所有项目
        count = len(_SESSION_WORKSPACES[session_id])
        del _SESSION_WORKSPACES[session_id]
        return count

    p = Path(project_path).resolve()
    current = _SESSION_WORKSPACES[session_id]
    before = len(current)
    _SESSION_WORKSPACES[session_id] = [x for x in current if x != p]
    return before - len(_SESSION_WORKSPACES[session_id])


def get_session_workspaces(session_id: str) -> list[Path]:
    """获取指定会话已打开的项目根路径列表。"""
    return list(_SESSION_WORKSPACES.get(session_id, []))


def has_session_files(session_id: str) -> bool:
    """检查会话是否有可访问的代码文件（打开的项目或上传的文件）。

    用于 Router 层拦截：若用户未打开项目且未上传文件，代码类问题直接拦截，
    避免无效调用 CodeGenSkill 的多轮 LLM。

    :param session_id: 会话 ID
    :return: True = 有可访问的代码文件；False = 无
    """
    if not session_id:
        return False
    # 1. 检查已打开的项目
    if get_session_workspaces(session_id):
        return True
    # 2. 检查上传的文件
    safe_sid = re.sub(r"[^a-zA-Z0-9._-]", "_", session_id)
    session_dir = Path(UPLOAD_DIR).resolve() / safe_sid
    if session_dir.exists():
        try:
            return any(f.is_file() for f in session_dir.iterdir())
        except Exception:
            return False
    return False


def _get_effective_allowed(session_id: str = None) -> list[Path]:
    """获取生效的白名单 = 内置 + 静态 + 会话动态。

    :param session_id: 会话 ID（为空则只返回内置+静态白名单）
    :return: 合并后的白名单 Path 列表
    """
    allowed = list(_BUILTIN_ALLOWED_PATHS)   # 内置功能目录（始终生效）
    allowed.extend(_ALLOWED_PATHS)          # 静态配置白名单
    if session_id:
        allowed.extend(_SESSION_WORKSPACES.get(session_id, []))
    return allowed


def _is_within_allowed(path: Path, session_id: str = None) -> bool:
    """判断路径是否在任一白名单目录内（含其自身）。

    :param path: 待校验路径（已 resolve）
    :param session_id: 会话 ID，传入时合并会话级动态白名单
    """
    for allowed in _get_effective_allowed(session_id):
        # path == allowed（自身）或 allowed 是 path 的父目录
        if path == allowed or allowed in path.parents:
            return True
    return False


def _is_blocked(path: Path) -> bool:
    """黑名单二次校验：即使白名单命中，系统目录也禁止。"""
    path_str = str(path).lower()
    for blocked in BLOCKED_DIRS:
        if path_str.startswith(blocked.lower()):
            return True
    return False


def _validate_path(filepath: str, session_id: str = None) -> Path:
    """校验文件路径安全性，返回 Path 对象。

    校验顺序：白名单（必须命中）→ 黑名单（必须未命中）→ 文件存在性

    :param filepath: 待校验文件路径
    :param session_id: 会话 ID，传入时合并会话级动态白名单
    :raises ValueError: 路径不在白名单、命中黑名单或文件不存在
    """
    p = Path(filepath).resolve()

    # 1. 白名单校验（必须命中任一允许的工作区）
    if not _is_within_allowed(p, session_id):
        allowed = _get_effective_allowed(session_id)
        allowed_str = ", ".join(str(a) for a in allowed) or "(空)"
        raise ValueError(
            f"路径不在允许的工作区内: {p}（允许: {allowed_str}）"
        )

    # 2. 黑名单双保险
    if _is_blocked(p):
        raise ValueError(f"禁止访问系统目录: {p}")

    return p


def _validate_write_path(filepath: str, session_id: str = None) -> Path:
    """校验写入路径（允许文件不存在，但路径必须在白名单内）。"""
    p = Path(filepath).resolve()

    # 1. 白名单校验
    if not _is_within_allowed(p, session_id):
        allowed = _get_effective_allowed(session_id)
        allowed_str = ", ".join(str(a) for a in allowed) or "(空)"
        raise ValueError(
            f"路径不在允许的工作区内: {p}（允许: {allowed_str}）"
        )

    # 2. 黑名单双保险
    if _is_blocked(p):
        raise ValueError(f"禁止访问系统目录: {p}")

    return p
