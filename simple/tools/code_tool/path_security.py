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
import threading
from pathlib import Path

from core.session_id import validate_session_id

from config import (
    CODE_HISTORY_DIR, BLOCKED_DIRS, ALLOWED_WORKSPACES,
    UPLOAD_DIR,
    USER_PROJECTS_DIR,
    WORKSPACE_MAX_PROJECTS,
    WORKSPACE_ALLOWED_ROOTS,
    WORKSPACE_HIDDEN_DIRS,
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
# 内置白名单：当前会话自己的功能目录（无需用户打开项目即可访问）
# ------------------------------------------------------------------
def _get_session_builtin_paths(session_id: str = None) -> list[Path]:
    """Return only the upload/history directories owned by this session."""
    if not session_id:
        return []
    validate_session_id(session_id)
    return [
        Path(UPLOAD_DIR).resolve() / session_id,
        Path(CODE_HISTORY_DIR).resolve() / session_id,
    ]


# ------------------------------------------------------------------
# 会话级动态白名单（打开项目时动态扩展）
# ------------------------------------------------------------------
# 结构：{session_id: [Path(项目根1), Path(项目根2), ...]}
# 内存维护，进程重启清空（如需持久化可后续接入 SQLite）
_SESSION_WORKSPACES: dict[str, list[Path]] = {}

# 会话级独立打开文件表（前端打开单个文件、不在已打开项目目录内时使用）
# 结构：{session_id: [Path(文件1), Path(文件2), ...]}
_SESSION_OPEN_FILES: dict[str, list[Path]] = {}
_SESSION_WORKSPACE_LOCK = threading.RLock()


_USER_ID_RE = re.compile(r"^[a-z0-9-]{1,128}$")
_PRIVATE_PROJECT_ID_RE = re.compile(r"^project-[0-9a-f]{32}$")


def _is_same_or_descendant(path: Path, root: Path) -> bool:
    """Return whether a resolved path is the root itself or below it."""
    return path == root or root in path.parents


def _get_user_project_root(user_id: str) -> Path:
    """Return the server-side private project mirror root for one user."""
    if not isinstance(user_id, str) or not _USER_ID_RE.fullmatch(user_id):
        raise ValueError("用户标识无效")
    return (Path(USER_PROJECTS_DIR).resolve() / user_id).resolve()


def get_private_project_id_for_workspace(
    project_path: str | Path,
    user_id: str,
) -> str | None:
    """Return the ID only for this owner's exact private mirror files root."""
    try:
        user_root = _get_user_project_root(user_id)
        candidate = Path(project_path).resolve()
        relative = candidate.relative_to(user_root)
    except (OSError, RuntimeError, TypeError, ValueError):
        return None

    if len(relative.parts) != 3:
        return None
    projects_component, project_id, files_component = relative.parts
    if projects_component != "projects" or files_component != "files":
        return None
    if _PRIVATE_PROJECT_ID_RE.fullmatch(project_id) is None:
        return None
    return project_id


def add_session_workspace(
    session_id: str,
    project_path: str,
    user_id: str = None,
) -> Path:
    """打开项目时调用，把项目根路径加入会话级白名单。

    :param session_id: 会话 ID
    :param project_path: 项目根目录绝对路径
    :return: 规范化后的 Path 对象
    :raises ValueError: 路径不存在、不是目录、超限、命中黑名单或前缀不允许
    """
    validate_session_id(session_id)

    p = Path(project_path).resolve()

    # Web 权限先于文件系统探测，避免用错误信息枚举服务器路径。
    if user_id:
        user_root = _get_user_project_root(user_id)
        if not _is_same_or_descendant(p, user_root):
            raise ValueError("项目不存在或无权访问")

    # 1. 必须存在且是目录
    if not p.exists():
        raise ValueError(f"项目目录不存在: {p}")
    if not p.is_dir():
        raise ValueError(f"路径不是目录: {p}")

    # 2. 黑名单校验（系统目录禁止）
    if _is_blocked(p):
        raise ValueError(f"禁止访问系统目录: {p}")

    # 3. Web 用户只能打开浏览器同步到其私有目录的服务器副本。
    if not user_id and WORKSPACE_ALLOWED_ROOTS:
        allowed_prefix = any(
            _is_same_or_descendant(p, Path(root).resolve())
            for root in WORKSPACE_ALLOWED_ROOTS
        )
        if not allowed_prefix:
            raise ValueError(
                f"项目路径不在允许的根目录前缀内: {p}（允许前缀: {WORKSPACE_ALLOWED_ROOTS}）"
            )

    # 4. 防止重复加入
    with _SESSION_WORKSPACE_LOCK:
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


def add_session_open_file(
    session_id: str,
    file_path: str,
    user_id: str = None,
) -> Path:
    """前端打开独立文件时调用，把文件 + 所在目录加入白名单。

    与 add_session_workspace 的差异：
    - 接受文件路径（不要求是目录）
    - 自动把文件父目录也加入 _SESSION_WORKSPACES（让 list_dir/grep_code 也能扫描到）
    - 同时记录到 _SESSION_OPEN_FILES（标记为"用户直接打开的文件"，Planner 优先推荐）

    :param session_id: 会话 ID
    :param file_path: 单个文件的绝对路径
    :return: 规范化后的 Path 对象
    :raises ValueError: 路径不存在、不是文件、命中隐藏目录/黑名单、前缀不允许或超限
    """
    validate_session_id(session_id)

    p = Path(file_path).resolve()

    # Web 权限先于文件系统探测，避免用错误信息枚举服务器路径。
    if user_id:
        user_root = _get_user_project_root(user_id)
        if not _is_same_or_descendant(p, user_root):
            raise ValueError("文件不存在或无权访问")

    # 1. 必须存在且是文件
    if not p.exists():
        raise ValueError(f"文件不存在: {p}")
    if not p.is_file():
        raise ValueError(f"路径不是文件: {p}")

    # 2. 黑名单校验（系统目录禁止，复用 _is_blocked）
    if _is_blocked(p):
        raise ValueError(f"禁止访问系统目录: {p}")

    # 3. 隐藏目录穿越校验（复用 _contains_hidden_dir_component）
    if _contains_hidden_dir_component(p):
        hidden_hit = next(
            (part for part in p.parts if part.lower() in _HIDDEN_DIR_NAMES_LOWER),
            None,
        )
        raise ValueError(
            f"禁止访问隐藏目录/文件: {p}（命中敏感目录: {hidden_hit}）"
        )

    # 4. Web 用户只能打开其服务器私有项目副本中的文件。
    if not user_id and WORKSPACE_ALLOWED_ROOTS:
        allowed_prefix = any(
            _is_same_or_descendant(p, Path(root).resolve())
            for root in WORKSPACE_ALLOWED_ROOTS
        )
        if not allowed_prefix:
            raise ValueError(
                f"文件路径不在允许的根目录前缀内: {p}（允许前缀: {WORKSPACE_ALLOWED_ROOTS}）"
            )

    # 5. 把文件所在目录加入目录白名单（让 grep_code/list_dir 可用）
    parent_dir = p.parent
    add_session_workspace(session_id, str(parent_dir), user_id=user_id)

    # 6. 独立文件去重记录
    with _SESSION_WORKSPACE_LOCK:
        current_files = _SESSION_OPEN_FILES.setdefault(session_id, [])
        if p in current_files:
            return p  # 已存在，幂等返回

        # 7. 文件数量限制（复用 WORKSPACE_MAX_PROJECTS）
        if len(current_files) >= WORKSPACE_MAX_PROJECTS:
            raise ValueError(
                f"会话已打开 {len(current_files)} 个独立文件，达到上限 {WORKSPACE_MAX_PROJECTS}"
            )

        current_files.append(p)
    return p


def remove_session_workspace(session_id: str, project_path: str = None) -> int:
    """关闭项目时调用，移除会话级白名单。

    :param session_id: 会话 ID
    :param project_path: 指定关闭的项目路径；为空时关闭该会话所有项目
    :return: 移除的项目数（含独立打开文件数）
    """
    validate_session_id(session_id)
    with _SESSION_WORKSPACE_LOCK:
        if session_id not in _SESSION_WORKSPACES and session_id not in _SESSION_OPEN_FILES:
            return 0

        if project_path is None:
            # 关闭该会话所有项目 + 同步清空独立打开文件表
            count = len(_SESSION_WORKSPACES.get(session_id, []))
            count += len(_SESSION_OPEN_FILES.pop(session_id, []))
            if session_id in _SESSION_WORKSPACES:
                del _SESSION_WORKSPACES[session_id]
            return count

        p = Path(project_path).resolve()
        current = _SESSION_WORKSPACES.get(session_id, [])
        before = len(current)
        remaining = [x for x in current if x != p]
        if remaining:
            _SESSION_WORKSPACES[session_id] = remaining
        else:
            _SESSION_WORKSPACES.pop(session_id, None)
        return before - len(remaining)


def remove_workspace_from_all_sessions(project_path: str) -> int:
    """Remove one exact project and files below it from every session."""
    project = Path(project_path).resolve()
    removed = 0
    with _SESSION_WORKSPACE_LOCK:
        for session_id, workspaces in list(_SESSION_WORKSPACES.items()):
            remaining = [workspace for workspace in workspaces if workspace != project]
            removed += len(workspaces) - len(remaining)
            if remaining:
                _SESSION_WORKSPACES[session_id] = remaining
            else:
                _SESSION_WORKSPACES.pop(session_id, None)
        for session_id, open_files in list(_SESSION_OPEN_FILES.items()):
            remaining = [
                filepath
                for filepath in open_files
                if not _is_same_or_descendant(filepath, project)
            ]
            removed += len(open_files) - len(remaining)
            if remaining:
                _SESSION_OPEN_FILES[session_id] = remaining
            else:
                _SESSION_OPEN_FILES.pop(session_id, None)
    return removed


def get_session_workspaces(session_id: str) -> list[Path]:
    """获取指定会话已打开的项目根路径列表。"""
    validate_session_id(session_id)
    with _SESSION_WORKSPACE_LOCK:
        return list(_SESSION_WORKSPACES.get(session_id, []))


def get_session_open_files(session_id: str) -> list[Path]:
    """获取指定会话中，前端单独打开的文件列表（用于 Planner 优先提示）。"""
    validate_session_id(session_id)
    with _SESSION_WORKSPACE_LOCK:
        return list(_SESSION_OPEN_FILES.get(session_id, []))


def has_session_files(session_id: str) -> bool:
    """检查会话是否有可访问的代码文件（打开的项目或独立文件或上传的文件）。

    用于 Router 层拦截：若用户未打开项目且未上传文件，代码类问题直接拦截，
    避免无效调用 CodeGenSkill 的多轮 LLM。

    :param session_id: 会话 ID
    :return: True = 有可访问的代码文件；False = 无
    """
    if not session_id:
        return False
    validate_session_id(session_id)
    # 1. 检查已打开的项目
    if get_session_workspaces(session_id):
        return True
    # 2. 检查独立打开的文件（★ 新增：前端单文件打开也算"有文件"）
    if get_session_open_files(session_id):
        return True
    # 3. 检查上传的文件
    session_dir = Path(UPLOAD_DIR).resolve() / session_id
    if session_dir.exists():
        try:
            return any(f.is_file() for f in session_dir.iterdir())
        except Exception:
            return False
    return False


def _get_effective_allowed(session_id: str = None) -> list[Path]:
    """获取生效的白名单 = 内置 + 静态 + 会话动态（含目录 + 独立文件）。

    :param session_id: 会话 ID（为空则只返回内置+静态白名单）
    :return: 合并后的白名单 Path 列表
    """
    allowed = _get_session_builtin_paths(session_id)
    # ALLOWED_WORKSPACES is a local CLI/admin compatibility setting. Web
    # sessions must explicitly bind a path inside their private project root.
    if not session_id or session_id == "cli-default":
        allowed.extend(_ALLOWED_PATHS)
    if session_id:
        validate_session_id(session_id)
        with _SESSION_WORKSPACE_LOCK:
            allowed.extend(_SESSION_WORKSPACES.get(session_id, []))
            # ★ 新增：把独立打开的文件也加入白名单（_is_within_allowed 用 path == allowed 命中自身）
            allowed.extend(_SESSION_OPEN_FILES.get(session_id, []))
    return allowed


def _is_within_allowed(path: Path, session_id: str = None) -> bool:
    """判断路径是否在任一白名单目录内（含其自身）。

    :param path: 待校验路径（已 resolve）
    :param session_id: 会话 ID，传入时合并会话级动态白名单
    """
    for allowed in _get_effective_allowed(session_id):
        # Path comparison follows the host filesystem semantics: case-insensitive
        # on Windows and case-sensitive on POSIX, without hard-coded separators.
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


# WORKSPACE_HIDDEN_DIRS 转小写集合，路径段命中即视为隐藏目录组件（含 .venv/.git/__pycache__/node_modules 等）
_HIDDEN_DIR_NAMES_LOWER = {d.lower() for d in WORKSPACE_HIDDEN_DIRS}


def _contains_hidden_dir_component(path: Path) -> bool:
    """判断路径中是否包含 WORKSPACE_HIDDEN_DIRS 定义的隐藏目录组件（任一段命中即 True）。

    即使父目录在白名单中（如用户打开了项目根），只要路径穿过 .venv/.git/node_modules 等隐藏目录，
    就视为不安全（防止 Agent 改虚拟环境/读 git 凭据/遍历 node_modules）。
    对路径的所有 components（含文件名）统一对比，确保即使伪装成文件名的敏感名字也被拦住。
    """
    return any(part.lower() in _HIDDEN_DIR_NAMES_LOWER for part in path.parts)


def _validate_path(filepath: str, session_id: str = None) -> Path:
    """校验文件路径安全性，返回 Path 对象。

    校验顺序：白名单（必须命中）→ 隐藏目录组件（必须未命中）→ 黑名单（必须未命中）

    :param filepath: 待校验文件路径
    :param session_id: 会话 ID，传入时合并会话级动态白名单
    :raises ValueError: 路径不在白名单、穿越隐藏目录、命中黑名单或文件不存在
    """
    p = Path(filepath).resolve()

    # 1. 白名单校验（必须命中任一允许的工作区）
    if not _is_within_allowed(p, session_id):
        raise ValueError("路径不存在或无权访问")

    # 2. 隐藏目录穿越校验：即使白名单命中，.venv/.git/node_modules 等也不可访问
    if _contains_hidden_dir_component(p):
        hidden_hit = next((part for part in p.parts if part.lower() in _HIDDEN_DIR_NAMES_LOWER), None)
        raise ValueError(
            f"禁止访问隐藏目录/文件: {p}（命中敏感目录: {hidden_hit}）"
        )

    # 3. 黑名单双保险（系统目录 BLOCKED_DIRS）
    if _is_blocked(p):
        raise ValueError(f"禁止访问系统目录: {p}")

    return p


def _validate_write_path(filepath: str, session_id: str = None) -> Path:
    """校验写入路径（允许文件不存在，但路径必须在白名单内且不穿越隐藏目录）。"""
    p = Path(filepath).resolve()

    # 1. 白名单校验
    if not _is_within_allowed(p, session_id):
        raise ValueError("路径不存在或无权访问")

    # 2. 隐藏目录穿越校验（写入路径也要拦：防止把敏感文件写进 .git 凭据/破坏虚拟环境）
    if _contains_hidden_dir_component(p):
        hidden_hit = next((part for part in p.parts if part.lower() in _HIDDEN_DIR_NAMES_LOWER), None)
        raise ValueError(
            f"禁止写入隐藏目录/文件: {p}（命中敏感目录: {hidden_hit}）"
        )

    # 3. 黑名单双保险
    if _is_blocked(p):
        raise ValueError(f"禁止访问系统目录: {p}")

    return p
