# core/project_routes.py
# ========== 项目管理 HTTP 路由 ==========
#
# 设计原则：
#   1. 与 auth_routes.py 同级同模式：APIRouter + 模块级函数
#   2. 认证与 CSRF 由 app.py 中间件统一处理，request.state.auth_session 已注入
#   3. 会话归属校验复用 _require_session_ownership
#   4. 工作区绑定延迟导入 tools.code_tool，避免循环依赖
#
# 路由列表：
#   GET  /api/projects/upload-policy     → 上传策略
#   POST /api/projects                   → 创建项目
#   GET  /api/projects                   → 列出项目
#   POST /api/projects/{id}/sync/upload  → 上传单个文件
#   POST /api/projects/{id}/sync/finish  → 完成同步
#   POST /api/projects/{id}/open         → 打开项目到会话
#   POST /api/projects/{id}/close        → 关闭项目
#   DELETE /api/projects/{id}            → 删除项目
#   GET  /api/projects/{id}/changes      → 获取待同步变更
#   POST /api/projects/{id}/sync/ack     → 确认同步
#   GET  /api/projects/{id}/file         → 下载文件

from __future__ import annotations

import hashlib
import time
from pathlib import Path

from fastapi import APIRouter, Request, Query
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict
from starlette.concurrency import run_in_threadpool

from config import (
    PROJECT_UPLOAD_MAX_FILE_SIZE,
    PROJECT_UPLOAD_MAX_PROJECT_SIZE,
    PROJECT_UPLOAD_MAX_PROJECT_FILES,
    PROJECT_UPLOAD_MAX_BATCH_FILES,
    PROJECT_UPLOAD_MAX_BATCH_SIZE,
    PROJECT_UPLOAD_ALLOWED_EXTENSIONS,
    PROJECT_UPLOAD_ALLOWED_FILENAMES,
    PROJECT_UPLOAD_EXCLUDED_DIRECTORIES,
    PROJECT_UPLOAD_EXCLUDED_FILENAMES,
    PROJECT_UPLOAD_EXCLUDED_PREFIXES,
    PROJECT_UPLOAD_EXCLUDED_SUFFIXES,
)
from core.project_store import (
    ProjectDeleteConfirmationError,
    ProjectFileIntegrityError,
    ProjectFileLimitError,
    ProjectFileValidationError,
    ProjectNameValidationError,
    ProjectStorageError,
    ProjectSyncConflictError,
    ProjectSyncAckEntry,
    ProjectSyncManifestEntry,
    SQLiteProjectStore,
    get_project_store,
)
from core.session_id import validate_session_id
from core.session_lifecycle import (
    SessionDeletingError, begin_request, require_current, run_if_current,
)


router = APIRouter(prefix="/api/projects", tags=["项目"])


# ===================== 延迟导入（避免循环依赖） =====================

def _bind_session_workspace(session_id: str, project_path: str, user_id: str):
    """把项目 files 目录加入会话白名单。"""
    from tools.code_tool.path_security import add_session_workspace
    return add_session_workspace(session_id, project_path, user_id)


def _remove_session_project_workspace(session_id: str, project_path: str) -> int:
    """从单个会话移除项目白名单。"""
    from tools.code_tool.path_security import remove_session_workspace
    return remove_session_workspace(session_id, project_path)


def _remove_project_workspaces(project_path: str) -> int:
    """从所有会话移除项目白名单。"""
    from tools.code_tool.path_security import remove_workspace_from_all_sessions
    return remove_workspace_from_all_sessions(project_path)


def _cancel_project_pending(session_ids: list[str], project_path: str) -> int:
    """取消项目下所有待确认修改。"""
    from tools.code_tool.diff_preview import cancel_modifications_under_path
    return cancel_modifications_under_path(session_ids, project_path)


def _delete_project_snapshots(session_ids: list[str], project_path: str) -> int:
    """删除项目下所有快照。"""
    from tools.code_tool.snapshot import delete_snapshots_under_path
    return delete_snapshots_under_path(session_ids, project_path)


async def _require_session_ownership(request: Request, session_id: str) -> None:
    """会话归属校验。"""
    from core.auth_routes import ensure_session_ownership
    await ensure_session_ownership(request.state.auth_session, session_id)


# ===================== Pydantic 模型 =====================

class ProjectCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str


class ProjectOpenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str


class ProjectCloseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str


class ProjectDeleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirmation_name: str


class ProjectSyncFinishFileItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    relative_path: str
    sha256: str
    size: int


class ProjectSyncFinishRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    files: list[ProjectSyncFinishFileItem]


class ProjectSyncAckFileItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    relative_path: str
    server_sha256: str | None


class ProjectSyncAckRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    files: list[ProjectSyncAckFileItem]


# ===================== 错误映射 =====================

def _safe_500() -> JSONResponse:
    return JSONResponse({"error": "项目服务暂时不可用"}, status_code=500)


def _map_store_error(exc: Exception) -> JSONResponse | None:
    """把存储层异常映射为 HTTP 响应。返回 None 表示不处理。"""
    if isinstance(exc, (ProjectFileValidationError, ProjectFileIntegrityError)):
        return JSONResponse({"error": str(exc)}, status_code=400)
    if isinstance(exc, ProjectSyncConflictError):
        return JSONResponse({"error": str(exc)}, status_code=409)
    if isinstance(exc, ProjectFileLimitError):
        return JSONResponse({"error": str(exc)}, status_code=413)
    if isinstance(exc, ProjectNameValidationError):
        return JSONResponse({"error": str(exc)}, status_code=400)
    if isinstance(exc, ProjectDeleteConfirmationError):
        return JSONResponse({"error": "项目名称确认不匹配"}, status_code=400)
    if isinstance(exc, (ProjectStorageError, RuntimeError)):
        return _safe_500()
    if isinstance(exc, ValueError) and "/private" in str(exc):
        return _safe_500()
    return None


# ===================== 端点 =====================

@router.get("/upload-policy")
async def get_upload_policy(request: Request):
    """返回项目上传策略（前端扫描文件时使用）。"""
    return {
        "max_file_size": PROJECT_UPLOAD_MAX_FILE_SIZE,
        "max_project_size": PROJECT_UPLOAD_MAX_PROJECT_SIZE,
        "max_project_files": PROJECT_UPLOAD_MAX_PROJECT_FILES,
        "max_batch_files": PROJECT_UPLOAD_MAX_BATCH_FILES,
        "max_batch_size": PROJECT_UPLOAD_MAX_BATCH_SIZE,
        "allowed_extensions": sorted(PROJECT_UPLOAD_ALLOWED_EXTENSIONS),
        "allowed_filenames": sorted(PROJECT_UPLOAD_ALLOWED_FILENAMES),
        "excluded_directories": sorted(PROJECT_UPLOAD_EXCLUDED_DIRECTORIES),
        "excluded_filenames": sorted(PROJECT_UPLOAD_EXCLUDED_FILENAMES),
        "excluded_prefixes": sorted(PROJECT_UPLOAD_EXCLUDED_PREFIXES),
        "excluded_suffixes": sorted(PROJECT_UPLOAD_EXCLUDED_SUFFIXES),
    }


@router.post("", status_code=201)
async def create_project(
    payload: ProjectCreateRequest,
    request: Request,
):
    """创建新项目。"""
    store = get_project_store()
    user_id = request.state.auth_session.user.id
    try:
        record = await run_in_threadpool(
            store.create_project, user_id, payload.name,
        )
    except Exception as exc:
        mapped = _map_store_error(exc)
        if mapped:
            return mapped
        raise
    return {
        "id": record.id,
        "name": record.name,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
    }


@router.get("")
async def list_projects(request: Request):
    """列出当前用户所有项目。"""
    store = get_project_store()
    user_id = request.state.auth_session.user.id
    try:
        records = await run_in_threadpool(store.list_projects, user_id)
    except Exception as exc:
        mapped = _map_store_error(exc)
        if mapped:
            return mapped
        raise
    return [
        {
            "id": r.id,
            "name": r.name,
            "created_at": r.created_at,
            "updated_at": r.updated_at,
            "sync_status": r.sync_status,
            "sync_revision": r.sync_revision,
        }
        for r in records
    ]


@router.post("/{project_id}/sync/upload")
async def upload_project_file(
    project_id: str,
    request: Request,
    relative_path: str = Query(...),
    sha256: str = Query(...),
):
    """上传单个文件到项目镜像。"""
    store = get_project_store()
    user_id = request.state.auth_session.user.id

    # 流式读取请求体
    content_type = request.headers.get("content-type", "")
    if content_type != "application/octet-stream":
        return JSONResponse(
            {"error": "仅支持 application/octet-stream"},
            status_code=415,
        )

    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > PROJECT_UPLOAD_MAX_FILE_SIZE:
            return JSONResponse(
                {"error": "文件大小超过限制"},
                status_code=413,
            )
    content = bytes(body)

    try:
        record = await run_in_threadpool(
            store.upload_project_file,
            user_id, project_id, relative_path, content, sha256,
        )
    except Exception as exc:
        import traceback as _tb
        print(f"DEBUG upload_project_file exc: {type(exc).__name__}: {exc}")
        _tb.print_exc()
        mapped = _map_store_error(exc)
        if mapped:
            return mapped
        raise
    return {
        "relative_path": record.relative_path,
        "sha256": record.server_sha256,
        "size": record.size,
        "status": "uploaded",
    }


@router.post("/{project_id}/sync/finish")
async def finish_project_sync(
    project_id: str,
    payload: ProjectSyncFinishRequest,
    request: Request,
):
    """完成项目同步。"""
    store = get_project_store()
    user_id = request.state.auth_session.user.id
    entries = [
        ProjectSyncManifestEntry(
            relative_path=item.relative_path,
            sha256=item.sha256,
            size=item.size,
        )
        for item in payload.files
    ]
    try:
        result = await run_in_threadpool(
            store.finish_project_sync,
            user_id, project_id, entries,
        )
    except Exception as exc:
        mapped = _map_store_error(exc)
        if mapped:
            return mapped
        raise
    return {
        "status": "finished",
        "project_id": result.project_id,
        "sync_revision": result.sync_revision,
        "file_count": result.file_count,
        "total_size": result.total_size,
        "deleted": result.deleted,
        "preserved_pending": result.preserved_pending,
    }


@router.post("/{project_id}/open")
async def open_project(
    project_id: str,
    payload: ProjectOpenRequest,
    request: Request,
):
    """打开项目到会话，把 files 目录加入白名单。"""
    store = get_project_store()
    user_id = request.state.auth_session.user.id
    session_id = payload.session_id
    validate_session_id(session_id)
    session_generation = begin_request(session_id)
    await _require_session_ownership(request, session_id)

    try:
        record = await run_in_threadpool(
            store.get_project, user_id, project_id,
        )
    except Exception as exc:
        mapped = _map_store_error(exc)
        if mapped:
            return mapped
        raise

    if record is None:
        return JSONResponse({"error": "项目不存在"}, status_code=404)
    if record.sync_status != "ready":
        return JSONResponse(
            {"error": "项目同步尚未完成"},
            status_code=409,
        )

    try:
        files_dir = await run_in_threadpool(
            store.get_files_dir, user_id, project_id,
        )
    except Exception as exc:
        mapped = _map_store_error(exc)
        if mapped:
            return mapped
        raise

    def _do_bind():
        return _bind_session_workspace(
            session_id, str(files_dir), user_id,
        )

    try:
        run_if_current(session_id, session_generation, _do_bind)
    except SessionDeletingError:
        raise

    return {
        "id": record.id,
        "name": record.name,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
        "session_id": session_id,
        "status": "opened",
    }


@router.post("/{project_id}/close")
async def close_project(
    project_id: str,
    payload: ProjectCloseRequest,
    request: Request,
):
    """从会话关闭项目。"""
    store = get_project_store()
    user_id = request.state.auth_session.user.id
    session_id = payload.session_id
    validate_session_id(session_id)
    session_generation = begin_request(session_id)
    await _require_session_ownership(request, session_id)

    try:
        record = await run_in_threadpool(
            store.get_project, user_id, project_id,
        )
    except Exception as exc:
        mapped = _map_store_error(exc)
        if mapped:
            return mapped
        raise

    if record is None:
        return JSONResponse({"error": "项目不存在"}, status_code=404)

    try:
        files_dir = await run_in_threadpool(
            store.get_files_dir, user_id, project_id,
        )
    except Exception as exc:
        mapped = _map_store_error(exc)
        if mapped:
            return mapped
        raise

    def _do_unbind():
        return _remove_session_project_workspace(session_id, str(files_dir))

    try:
        removed = run_if_current(session_id, session_generation, _do_unbind)
    except SessionDeletingError:
        raise

    return {
        "status": "closed",
        "project_id": project_id,
        "session_id": session_id,
        "removed_count": removed,
    }


@router.delete("/{project_id}")
async def delete_project(
    project_id: str,
    payload: ProjectDeleteRequest,
    request: Request,
):
    """删除项目（含文件目录、DB 记录、白名单、快照、待确认修改）。"""
    store = get_project_store()
    user_id = request.state.auth_session.user.id

    try:
        record = await run_in_threadpool(
            store.get_project, user_id, project_id,
        )
    except Exception as exc:
        mapped = _map_store_error(exc)
        if mapped:
            return mapped
        raise

    if record is None:
        return JSONResponse({"error": "项目不存在"}, status_code=404)

    try:
        files_dir = await run_in_threadpool(
            store.get_files_dir, user_id, project_id,
        )
    except Exception as exc:
        mapped = _map_store_error(exc)
        if mapped:
            return mapped
        raise

    # 先解绑所有会话中的白名单
    workspaces_removed = _remove_project_workspaces(str(files_dir))

    # 获取用户所有会话 ID
    from core.auth_store import get_auth_store
    auth_store = get_auth_store()
    session_ids = await run_in_threadpool(
        auth_store.list_user_chat_session_ids, user_id,
    )

    # 取消待确认修改
    pending_cancelled = _cancel_project_pending(session_ids, str(files_dir))
    # 删除快照
    snapshots_deleted = _delete_project_snapshots(session_ids, str(files_dir))

    # 删除项目
    try:
        result = await run_in_threadpool(
            store.delete_project,
            user_id, project_id, payload.confirmation_name,
        )
    except ProjectDeleteConfirmationError:
        return JSONResponse(
            {"error": "项目名称确认不匹配"},
            status_code=400,
        )
    except Exception as exc:
        mapped = _map_store_error(exc)
        if mapped:
            return mapped
        raise

    local_deleted = result.status == "deleted"

    return {
        "status": result.status,
        "project_id": result.project_id,
        "local_files_deleted": local_deleted,
        "workspaces_removed": workspaces_removed,
        "pending_cancelled": pending_cancelled,
        "snapshots_deleted": snapshots_deleted,
    }


@router.get("/{project_id}/changes")
async def list_project_changes(
    project_id: str,
    request: Request,
):
    """获取待同步变更列表。"""
    store = get_project_store()
    user_id = request.state.auth_session.user.id

    try:
        revision, changes = await run_in_threadpool(
            store.list_project_changes, user_id, project_id,
        )
    except Exception as exc:
        mapped = _map_store_error(exc)
        if mapped:
            return mapped
        raise

    return {
        "project_id": project_id,
        "sync_revision": revision,
        "count": len(changes),
        "changes": [
            {
                "relative_path": c.relative_path,
                "baseline_sha256": c.baseline_sha256,
                "server_sha256": c.server_sha256,
                "size": c.size,
                "sync_state": c.sync_state,
                "change_type": c.change_type,
            }
            for c in changes
        ],
    }


@router.post("/{project_id}/sync/ack")
async def ack_project_sync(
    project_id: str,
    payload: ProjectSyncAckRequest,
    request: Request,
):
    """确认同步：baseline=server, sync_state=synced。"""
    store = get_project_store()
    user_id = request.state.auth_session.user.id
    entries = [
        ProjectSyncAckEntry(
            relative_path=item.relative_path,
            server_sha256=item.server_sha256,
        )
        for item in payload.files
    ]
    try:
        result = await run_in_threadpool(
            store.ack_project_sync,
            user_id, project_id, entries,
        )
    except Exception as exc:
        mapped = _map_store_error(exc)
        if mapped:
            return mapped
        raise
    return {
        "status": "acknowledged",
        "project_id": result.project_id,
        "sync_revision": result.sync_revision,
        "count": len(result.acked),
        "acked": result.acked,
    }


@router.get("/{project_id}/file")
async def download_project_file(
    project_id: str,
    request: Request,
    relative_path: str = Query(...),
    server_sha256: str = Query(...),
):
    """下载项目文件。"""
    store = get_project_store()
    user_id = request.state.auth_session.user.id

    try:
        download = await run_in_threadpool(
            store.download_project_change,
            user_id, project_id, relative_path, server_sha256,
        )
    except Exception as exc:
        mapped = _map_store_error(exc)
        if mapped:
            return mapped
        raise

    return Response(
        content=download.content,
        media_type="application/octet-stream",
        headers={
            "Cache-Control": "no-store",
            "ETag": f'"{download.server_sha256}"',
            "X-Content-SHA256": download.server_sha256,
        },
    )


__all__ = ["router"]
