# core/project_store.py
# ========== 项目存储层：SQLite + 文件系统镜像 ==========
#
# 职责：
#   1. 项目元数据 CRUD（projects 表）
#   2. 项目文件记录管理（project_files 表）
#   3. 文件系统镜像读写（projects_root/{user_id}/projects/{project_id}/files/）
#   4. 同步状态机：uploading → ready → pending_local_sync → synced
#
# 路径结构：
#   {projects_root}/{user_id}/projects/{project_id}/files/{relative_path}
#
# 与 auth_store 共享 auth.db，但用自己的连接和 RLock

from __future__ import annotations

import hashlib
import os
import re
import shutil
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from config import (
    AUTH_DB_PATH,
    USER_PROJECTS_DIR,
    PROJECT_UPLOAD_MAX_FILE_SIZE,
    PROJECT_UPLOAD_MAX_PROJECT_FILES,
    PROJECT_UPLOAD_MAX_PROJECT_SIZE,
    PROJECT_UPLOAD_ALLOWED_EXTENSIONS,
    PROJECT_UPLOAD_ALLOWED_FILENAMES,
    PROJECT_UPLOAD_EXCLUDED_DIRECTORIES,
    PROJECT_UPLOAD_EXCLUDED_FILENAMES,
    PROJECT_UPLOAD_EXCLUDED_PREFIXES,
    PROJECT_UPLOAD_EXCLUDED_SUFFIXES,
)


# ===================== 异常类 =====================

class ProjectStorageError(RuntimeError):
    """存储不可用（磁盘/数据库故障）。"""


class ProjectNameValidationError(ValueError):
    """项目名称校验失败。"""


class ProjectDeleteConfirmationError(ValueError):
    """项目删除确认名称不匹配。"""


class ProjectFileValidationError(ValueError):
    """文件路径或类型校验失败。"""


class ProjectFileIntegrityError(ValueError):
    """文件 SHA-256 校验失败。"""


class ProjectFileLimitError(ValueError):
    """文件大小或数量超限。"""


class ProjectSyncConflictError(ValueError):
    """同步状态冲突。"""


# ===================== dataclass =====================

@dataclass(frozen=True, slots=True)
class ProjectRecord:
    """项目元数据记录。"""
    id: str
    user_id: str
    name: str
    created_at: float
    updated_at: float
    sync_status: str = "ready"
    sync_revision: int = 0


@dataclass(frozen=True, slots=True)
class ProjectFileRecord:
    """项目文件记录。"""
    relative_path: str
    baseline_sha256: str | None
    server_sha256: str | None
    size: int
    sync_state: str  # synced | pending_local_sync | conflict


@dataclass(frozen=True, slots=True)
class ProjectDeleteResult:
    """项目删除结果。"""
    project_id: str
    status: str  # deleted | deletion_pending_cleanup


@dataclass(frozen=True, slots=True)
class ProjectSyncManifestEntry:
    """同步清单条目。"""
    relative_path: str
    sha256: str
    size: int


@dataclass(frozen=True, slots=True)
class ProjectSyncFinishResult:
    """同步完成结果。"""
    project_id: str
    sync_revision: int
    file_count: int
    total_size: int
    deleted: list[str] = field(default_factory=list)
    preserved_pending: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class ProjectSyncAckEntry:
    """同步确认条目。"""
    relative_path: str
    server_sha256: str | None


@dataclass(frozen=True, slots=True)
class ProjectSyncAckResult:
    """同步确认结果。"""
    project_id: str
    sync_revision: int
    acked: list[str]


@dataclass(frozen=True, slots=True)
class ProjectChangeRecord:
    """项目变更记录。"""
    relative_path: str
    baseline_sha256: str | None
    server_sha256: str | None
    size: int
    sync_state: str
    change_type: str  # created | modified | deleted


@dataclass(frozen=True, slots=True)
class ProjectFileDownload:
    """项目文件下载内容。"""
    relative_path: str
    content: bytes
    baseline_sha256: str | None
    server_sha256: str
    size: int


# ===================== 校验辅助 =====================

_USER_ID_RE = re.compile(r"^user-[0-9a-f]{32}$")
_PROJECT_ID_RE = re.compile(r"^project-[0-9a-f]{32}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_PROJECT_NAME_MAX_LEN = 128
_RELATIVE_PATH_MAX_BYTES = 1024
_RELATIVE_SEGMENT_MAX_BYTES = 255

_ALLOWED_EXT_SET = set(PROJECT_UPLOAD_ALLOWED_EXTENSIONS)
_ALLOWED_NAME_SET = set(PROJECT_UPLOAD_ALLOWED_FILENAMES)
_EXCLUDED_DIR_SET = set(PROJECT_UPLOAD_EXCLUDED_DIRECTORIES)
_EXCLUDED_NAME_SET = set(PROJECT_UPLOAD_EXCLUDED_FILENAMES)
_EXCLUDED_PREFIX_LIST = [p.lower() for p in PROJECT_UPLOAD_EXCLUDED_PREFIXES]
_EXCLUDED_SUFFIX_LIST = [s.lower() for s in PROJECT_UPLOAD_EXCLUDED_SUFFIXES]


def _validate_user_id(user_id: str) -> None:
    if not isinstance(user_id, str) or not _USER_ID_RE.fullmatch(user_id):
        raise ValueError("用户标识无效")


def _validate_project_id(project_id: str) -> None:
    if not isinstance(project_id, str) or not _PROJECT_ID_RE.fullmatch(project_id):
        raise ValueError("项目标识无效")


def _validate_sha256(value: str) -> None:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise ValueError("SHA-256 格式无效")


def _validate_project_name(name: str) -> str:
    """校验并规范化项目名称。"""
    if not isinstance(name, str):
        raise ProjectNameValidationError("项目名称必须为字符串")
    stripped = name.strip()
    if not stripped:
        raise ProjectNameValidationError("项目名称不能为空")
    if len(stripped) > _PROJECT_NAME_MAX_LEN:
        raise ProjectNameValidationError(
            f"项目名称长度必须为 1-{_PROJECT_NAME_MAX_LEN} 个字符"
        )
    if re.search(r"[\x00-\x1f\x7f-\x9f]", stripped):
        raise ProjectNameValidationError("项目名称不能包含控制字符")
    return stripped


def _validate_relative_path(relative_path: str) -> None:
    """校验相对路径安全性。"""
    if not isinstance(relative_path, str) or not relative_path:
        raise ProjectFileValidationError("文件相对路径不能为空")
    if "\\" in relative_path:
        raise ProjectFileValidationError("文件路径不能包含反斜杠")
    if relative_path.startswith("/"):
        raise ProjectFileValidationError("文件路径不能为绝对路径")
    if re.match(r"^[A-Za-z]:", relative_path):
        raise ProjectFileValidationError("文件路径不能为绝对路径")
    if re.search(r"[\x00-\x1f\x7f-\x9f]", relative_path):
        raise ProjectFileValidationError("文件路径不能包含控制字符")
    if len(relative_path.encode("utf-8")) > _RELATIVE_PATH_MAX_BYTES:
        raise ProjectFileValidationError("文件路径过长")
    components = relative_path.split("/")
    for component in components:
        if not component or component == "." or component == "..":
            raise ProjectFileValidationError("文件路径包含无效分段")
        if len(component.encode("utf-8")) > _RELATIVE_SEGMENT_MAX_BYTES:
            raise ProjectFileValidationError("文件路径分段过长")


def _check_file_acceptable(relative_path: str) -> None:
    """校验文件名/扩展名是否在上传白名单内。"""
    name = relative_path.rsplit("/", 1)[-1].lower()
    dot = name.rfind(".")
    ext = name[dot:] if dot > 0 else ""
    if name in _EXCLUDED_NAME_SET:
        raise ProjectFileValidationError("文件名在排除列表中")
    for prefix in _EXCLUDED_PREFIX_LIST:
        if name.startswith(prefix):
            raise ProjectFileValidationError("文件名前缀在排除列表中")
    for suffix in _EXCLUDED_SUFFIX_LIST:
        if name.endswith(suffix):
            raise ProjectFileValidationError("文件名后缀在排除列表中")
    if name not in _ALLOWED_NAME_SET and ext not in _ALLOWED_EXT_SET:
        raise ProjectFileValidationError("文件类型不在允许列表中")


def _is_excluded_dir(dir_name: str) -> bool:
    return dir_name.lower() in _EXCLUDED_DIR_SET


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()



def _is_text_content(content: bytes) -> bool:
    """检查内容是否为有效 UTF-8 文本且不含 NUL。"""
    try:
        text = content.decode("utf-8")
        if "\x00" in text:
            return False
        return True
    except UnicodeDecodeError:
        return False


# ===================== SQLiteProjectStore =====================

class SQLiteProjectStore:
    """项目元数据与文件镜像的持久化存储。"""

    def __init__(self, db_path: str | Path, projects_root: str | Path):
        self.db_path = Path(db_path)
        self.projects_root = Path(projects_root).resolve()
        self._lock = threading.RLock()
        self._cleanup_quarantine()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            str(self.db_path),
            timeout=5.0,
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    # ---- 路径解析 ----

    def _resolve_user_root(self, user_id: str) -> Path:
        _validate_user_id(user_id)
        root = (self.projects_root / user_id).resolve()
        if root.is_symlink():
            raise ProjectStorageError("项目存储不可用")
        return root

    def _resolve_project_dir(self, user_id: str, project_id: str) -> Path:
        _validate_user_id(user_id)
        _validate_project_id(project_id)
        user_root = self._resolve_user_root(user_id)
        projects_dir = (user_root / "projects").resolve()
        project_dir = (projects_dir / project_id).resolve()
        files_dir = (project_dir / "files").resolve()
        return files_dir

    def get_files_dir(self, user_id: str, project_id: str) -> Path:
        """返回项目 files 目录绝对路径，校验每段无符号链接。"""
        _validate_user_id(user_id)
        _validate_project_id(project_id)
        user_root = self._resolve_user_root(user_id)
        base = user_root
        for segment in ("projects", project_id, "files"):
            base = base / segment
            if base.is_symlink():
                raise ProjectStorageError("项目存储不可用")
        if not base.exists():
            raise ProjectStorageError("项目存储不可用")
        return base.resolve()

    # ---- 项目 CRUD ----

    def create_project(
        self,
        user_id: str,
        name: str,
        now: float | None = None,
    ) -> ProjectRecord:
        """创建项目：生成 ID、创建 files 目录、写入 DB。"""
        _validate_user_id(user_id)
        display_name = _validate_project_name(name)
        ts = time.time() if now is None else now
        project_id = f"project-{uuid.uuid4().hex}"

        # 创建文件目录
        user_root = self._resolve_user_root(user_id)
        files_dir = user_root / "projects" / project_id / "files"
        files_dir.mkdir(parents=True, exist_ok=True)

        try:
            with self._lock, self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO projects
                        (id, user_id, name, created_at, updated_at,
                         sync_status, sync_revision)
                    VALUES (?, ?, ?, ?, ?, 'uploading', 0)
                    """,
                    (project_id, user_id, display_name, ts, ts),
                )
        except sqlite3.Error as exc:
            # 清理刚创建的目录
            shutil.rmtree(files_dir.parent, ignore_errors=True)
            raise ProjectStorageError("项目存储不可用") from exc

        return ProjectRecord(
            id=project_id,
            user_id=user_id,
            name=display_name,
            created_at=ts,
            updated_at=ts,
            sync_status="uploading",
            sync_revision=0,
        )

    def list_projects(self, user_id: str) -> list[ProjectRecord]:
        """列出用户所有项目。"""
        _validate_user_id(user_id)
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, user_id, name, created_at, updated_at,
                       sync_status, sync_revision
                FROM projects
                WHERE user_id = ?
                ORDER BY created_at DESC, id ASC
                """,
                (user_id,),
            ).fetchall()
        return [self._row_to_project(r) for r in rows]

    def get_project(
        self,
        user_id: str,
        project_id: str,
    ) -> ProjectRecord | None:
        """获取单个项目；foreign owner 返回 None。"""
        _validate_user_id(user_id)
        _validate_project_id(project_id)
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT id, user_id, name, created_at, updated_at,
                       sync_status, sync_revision
                FROM projects
                WHERE id = ? AND user_id = ?
                """,
                (project_id, user_id),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_project(row)

    def delete_project(
        self,
        user_id: str,
        project_id: str,
        confirmation_name: str,
    ) -> ProjectDeleteResult:
        """删除项目：确认名称 → 隔离区 rename → DB 删除 → rmtree。"""
        _validate_user_id(user_id)
        _validate_project_id(project_id)

        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT name FROM projects WHERE id = ? AND user_id = ?",
                (project_id, user_id),
            ).fetchone()
            if row is None:
                raise ProjectStorageError("项目存储不可用")
            if confirmation_name != row["name"]:
                raise ProjectDeleteConfirmationError("项目名称确认不匹配")

            # 隔离区模式
            user_root = self._resolve_user_root(user_id)
            project_dir = user_root / "projects" / project_id
            quarantine_dir = user_root / ".deleting"
            quarantine_dir.mkdir(parents=True, exist_ok=True)
            temp_name = f"{project_id}-{uuid.uuid4().hex}"
            quarantine_path = quarantine_dir / temp_name

            rmtree_failed = False
            try:
                if project_dir.exists():
                    os.rename(str(project_dir), str(quarantine_path))
            except OSError:
                # rename 失败，尝试直接 rmtree
                shutil.rmtree(project_dir, ignore_errors=True)
                rmtree_failed = True

            try:
                conn.execute(
                    "DELETE FROM project_files WHERE project_id = ?",
                    (project_id,),
                )
                conn.execute(
                    "DELETE FROM projects WHERE id = ? AND user_id = ?",
                    (project_id, user_id),
                )
            except sqlite3.Error:
                # DB 失败，尝试恢复目录
                if quarantine_path.exists():
                    try:
                        os.rename(str(quarantine_path), str(project_dir))
                    except OSError:
                        pass
                raise ProjectStorageError("项目存储不可用")

        # DB 成功后删除隔离区目录
        if not rmtree_failed and quarantine_path.exists():
            try:
                shutil.rmtree(quarantine_path)
            except OSError:
                return ProjectDeleteResult(project_id=project_id, status="deletion_pending_cleanup")

        return ProjectDeleteResult(project_id=project_id, status="deleted")

    # ---- 文件上传 ----

    def upload_project_file(
        self,
        user_id: str,
        project_id: str,
        relative_path: str,
        content: bytes,
        expected_sha256: str,
    ) -> ProjectFileRecord:
        """上传单个文件到项目镜像。"""
        _validate_user_id(user_id)
        _validate_project_id(project_id)
        _validate_relative_path(relative_path)
        _validate_sha256(expected_sha256)

        if not isinstance(content, bytes):
            raise ProjectFileValidationError("文件内容必须是字节")
        if len(content) > PROJECT_UPLOAD_MAX_FILE_SIZE:
            raise ProjectFileLimitError("文件大小超过限制")

        # SHA-256 校验
        actual_sha256 = _sha256_hex(content)
        if actual_sha256 != expected_sha256:
            raise ProjectFileIntegrityError("SHA-256 校验失败")

        # 文件名/扩展名白名单
        _check_file_acceptable(relative_path)

        # UTF-8 文本校验
        if not _is_text_content(content):
            raise ProjectFileValidationError("文件内容不是有效的 UTF-8 文本")

        files_dir = self.get_files_dir(user_id, project_id)
        target = files_dir / relative_path

        # 防止路径穿越
        try:
            target.resolve().relative_to(files_dir)
        except (ValueError, RuntimeError):
            raise ProjectFileValidationError("文件路径越界")

        # 校验项目总大小和文件数
        with self._lock, self._connect() as conn:
            # 获取项目信息
            proj = conn.execute(
                "SELECT sync_status FROM projects WHERE id = ? AND user_id = ?",
                (project_id, user_id),
            ).fetchone()
            if proj is None:
                raise ProjectStorageError("项目存储不可用")

            # 已有记录
            existing = conn.execute(
                """
                SELECT relative_path, baseline_sha256, server_sha256,
                       size, sync_state
                FROM project_files
                WHERE project_id = ? AND relative_path = ?
                """,
                (project_id, relative_path),
            ).fetchone()

            # 计算总大小
            total_size_row = conn.execute(
                "SELECT COALESCE(SUM(size), 0) AS total FROM project_files WHERE project_id = ?",
                (project_id,),
            ).fetchone()
            current_total = total_size_row["total"]

            file_count_row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM project_files WHERE project_id = ?",
                (project_id,),
            ).fetchone()
            current_count = file_count_row["cnt"]

            # 替换时减去旧大小
            old_size = existing["size"] if existing else 0
            new_total = current_total - old_size + len(content)
            if new_total > PROJECT_UPLOAD_MAX_PROJECT_SIZE:
                raise ProjectFileLimitError("项目总大小超过限制")

            if existing is None:
                if current_count >= PROJECT_UPLOAD_MAX_PROJECT_FILES:
                    raise ProjectFileLimitError("项目文件数量超过限制")
            elif existing["sync_state"] != "synced":
                raise ProjectSyncConflictError(
                    f"文件 {relative_path} 处于未同步状态"
                )

            # 检查磁盘上是否已存在未跟踪的文件
            if existing is None and target.exists():
                raise ProjectSyncConflictError(
                    f"文件 {relative_path} 在磁盘上已存在但未被跟踪"
                )

            # 原子写入：临时文件 + os.replace
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = target.parent / f".upload-{uuid.uuid4().hex}.tmp"
            old_content = None
            try:
                if target.exists():
                    old_content = target.read_bytes()
                tmp_path.write_bytes(content)
                os.replace(str(tmp_path), str(target))
            except OSError as exc:
                if tmp_path.exists():
                    tmp_path.unlink(missing_ok=True)
                raise ProjectStorageError("项目存储不可用") from exc

            # DB upsert
            try:
                conn.execute(
                    """
                    INSERT INTO project_files
                        (project_id, relative_path, baseline_sha256,
                         server_sha256, size, sync_state)
                    VALUES (?, ?, ?, ?, ?, 'synced')
                    ON CONFLICT(project_id, relative_path) DO UPDATE SET
                        baseline_sha256 = excluded.baseline_sha256,
                        server_sha256 = excluded.server_sha256,
                        size = excluded.size,
                        sync_state = 'synced'
                    """,
                    (project_id, relative_path, actual_sha256, actual_sha256, len(content)),
                )
            except sqlite3.Error:
                # DB 失败，恢复文件
                if old_content is not None:
                    target.write_bytes(old_content)
                elif target.exists():
                    target.unlink(missing_ok=True)
                raise ProjectStorageError("项目存储不可用")

            # 如果项目已 ready，上传新文件后回退到 uploading
            if proj["sync_status"] == "ready":
                conn.execute(
                    "UPDATE projects SET sync_status = 'uploading', updated_at = ? WHERE id = ?",
                    (time.time(), project_id),
                )

        return ProjectFileRecord(
            relative_path=relative_path,
            baseline_sha256=existing["baseline_sha256"] if existing else None,
            server_sha256=actual_sha256,
            size=len(content),
            sync_state="synced",
        )

    # ---- 同步完成 ----

    def finish_project_sync(
        self,
        user_id: str,
        project_id: str,
        entries: list[ProjectSyncManifestEntry],
    ) -> ProjectSyncFinishResult:
        """完成项目同步：校验 manifest → 清理 → ready。"""
        _validate_user_id(user_id)
        _validate_project_id(project_id)

        # 校验 manifest
        seen_paths = set()
        manifest_map = {}
        for entry in entries:
            _validate_relative_path(entry.relative_path)
            _validate_sha256(entry.sha256)
            if not isinstance(entry.size, int) or entry.size < 0:
                raise ProjectFileValidationError("文件大小无效")
            if entry.relative_path in seen_paths:
                raise ProjectFileValidationError("manifest 包含重复路径")
            seen_paths.add(entry.relative_path)
            manifest_map[entry.relative_path] = entry

        files_dir = self.get_files_dir(user_id, project_id)

        with self._lock, self._connect() as conn:
            proj = conn.execute(
                "SELECT sync_status, sync_revision FROM projects WHERE id = ? AND user_id = ?",
                (project_id, user_id),
            ).fetchone()
            if proj is None:
                raise ProjectStorageError("项目存储不可用")

            # 获取 DB 中的所有文件记录
            db_rows = conn.execute(
                """
                SELECT relative_path, baseline_sha256, server_sha256,
                       size, sync_state
                FROM project_files WHERE project_id = ?
                """,
                (project_id,),
            ).fetchall()
            db_map = {r["relative_path"]: r for r in db_rows}

            # 校验：manifest 中的文件必须有 DB 记录且 sync_state=synced
            for path, entry in manifest_map.items():
                row = db_map.get(path)
                if row is None:
                    raise ProjectSyncConflictError(
                        f"文件 {path} 未上传"
                    )
                if row["sync_state"] != "synced":
                    raise ProjectSyncConflictError(
                        f"文件 {path} 处于未同步状态"
                    )

                # 重读磁盘验证 SHA-256
                disk_path = files_dir / path
                try:
                    disk_path.resolve().relative_to(files_dir)
                except (ValueError, RuntimeError):
                    raise ProjectFileValidationError("文件路径越界")
                if not disk_path.exists():
                    raise ProjectSyncConflictError(
                        f"文件 {path} 在磁盘上不存在"
                    )
                disk_content = disk_path.read_bytes()
                if _sha256_hex(disk_content) != entry.sha256:
                    raise ProjectFileIntegrityError(
                        f"文件 {path} 的 SHA-256 与 manifest 不匹配"
                    )

            # 清理 manifest 之外的文件
            deleted_paths = []
            preserved_pending = []

            for path, row in db_map.items():
                if path in manifest_map:
                    continue
                disk_path = files_dir / path
                if row["sync_state"] == "synced":
                    # 已跟踪已同步但不在 manifest → 删除
                    conn.execute(
                        "DELETE FROM project_files WHERE project_id = ? AND relative_path = ?",
                        (project_id, path),
                    )
                    try:
                        disk_path.unlink(missing_ok=True)
                    except OSError:
                        pass
                    deleted_paths.append(path)
                elif disk_path.exists():
                    # 未同步但磁盘存在 → preserved pending
                    preserved_pending.append(path)

            # 删除空目录
            for dirpath, dirnames, filenames in os.walk(files_dir, topdown=False):
                if dirpath == str(files_dir):
                    continue
                dp = Path(dirpath)
                try:
                    if not any(dp.iterdir()):
                        dp.rmdir()
                except OSError:
                    pass

            # 更新项目状态
            new_revision = proj["sync_revision"]
            if proj["sync_status"] != "ready" or True:
                # 首次 finish: revision 从 0 → 1
                # 幂等 finish: revision 不变
                if proj["sync_status"] != "ready":
                    new_revision = proj["sync_revision"] + 1
                conn.execute(
                    """
                    UPDATE projects
                    SET sync_status = 'ready', sync_revision = ?, updated_at = ?
                    WHERE id = ? AND user_id = ?
                    """,
                    (new_revision, time.time(), project_id, user_id),
                )

            # 统计
            count_row = conn.execute(
                "SELECT COUNT(*) AS cnt, COALESCE(SUM(size), 0) AS total "
                "FROM project_files WHERE project_id = ?",
                (project_id,),
            ).fetchone()

        return ProjectSyncFinishResult(
            project_id=project_id,
            sync_revision=new_revision,
            file_count=count_row["cnt"],
            total_size=count_row["total"],
            deleted=sorted(deleted_paths),
            preserved_pending=sorted(preserved_pending),
        )

    # ---- 变更检测 ----

    def list_project_changes(
        self,
        user_id: str,
        project_id: str,
    ) -> tuple[int, list[ProjectChangeRecord]]:
        """扫描磁盘 vs DB，分类 created/modified/deleted。"""
        _validate_user_id(user_id)
        _validate_project_id(project_id)
        files_dir = self.get_files_dir(user_id, project_id)

        # 扫描磁盘文件
        disk_files = {}  # relative_path -> (sha256, size)
        for dirpath, dirnames, filenames in os.walk(files_dir):
            # 过滤排除目录
            dirnames[:] = [
                d for d in dirnames
                if not _is_excluded_dir(d) and not d.startswith(".")
            ]
            for filename in filenames:
                if filename.startswith(".upload-") or filename.startswith(".sync-staging-"):
                    continue
                full = Path(dirpath) / filename
                if full.is_symlink():
                    continue
                try:
                    rel = full.relative_to(files_dir).as_posix()
                except (ValueError, RuntimeError):
                    continue
                try:
                    content = full.read_bytes()
                except OSError:
                    raise ProjectStorageError("项目存储不可用")
                if not _is_text_content(content):
                    continue
                _check_file_acceptable(rel)
                disk_files[rel] = (_sha256_hex(content), len(content))

        with self._lock, self._connect() as conn:
            proj = conn.execute(
                "SELECT sync_revision FROM projects WHERE id = ? AND user_id = ?",
                (project_id, user_id),
            ).fetchone()
            if proj is None:
                raise ProjectStorageError("项目存储不可用")

            db_rows = conn.execute(
                """
                SELECT relative_path, baseline_sha256, server_sha256,
                       size, sync_state
                FROM project_files WHERE project_id = ?
                """,
                (project_id,),
            ).fetchall()
            db_map = {r["relative_path"]: r for r in db_rows}

            changes = []
            new_revision = proj["sync_revision"]

            # 磁盘有、DB 有 → modified 或 unchanged
            for path, (disk_hash, disk_size) in disk_files.items():
                row = db_map.get(path)
                if row is None:
                    # created
                    changes.append(ProjectChangeRecord(
                        relative_path=path,
                        baseline_sha256=None,
                        server_sha256=disk_hash,
                        size=disk_size,
                        sync_state="pending_local_sync",
                        change_type="created",
                    ))
                elif row["server_sha256"] != disk_hash:
                    # modified
                    changes.append(ProjectChangeRecord(
                        relative_path=path,
                        baseline_sha256=row["baseline_sha256"],
                        server_sha256=disk_hash,
                        size=disk_size,
                        sync_state="pending_local_sync",
                        change_type="modified",
                    ))
                # else: unchanged, skip

            # DB 有、磁盘无 → deleted
            for path, row in db_map.items():
                if path not in disk_files:
                    changes.append(ProjectChangeRecord(
                        relative_path=path,
                        baseline_sha256=row["baseline_sha256"],
                        server_sha256=None,
                        size=0,
                        sync_state="pending_local_sync",
                        change_type="deleted",
                    ))

            # 更新 sync_state 和 revision
            if changes:
                new_revision = proj["sync_revision"] + 1
                for change in changes:
                    if change.change_type == "deleted":
                        # 删除 DB row
                        conn.execute(
                            "DELETE FROM project_files WHERE project_id = ? AND relative_path = ?",
                            (project_id, change.relative_path),
                        )
                    else:
                        # upsert pending 状态
                        conn.execute(
                            """
                            INSERT INTO project_files
                                (project_id, relative_path, baseline_sha256,
                                 server_sha256, size, sync_state)
                            VALUES (?, ?, ?, ?, ?, 'pending_local_sync')
                            ON CONFLICT(project_id, relative_path) DO UPDATE SET
                                server_sha256 = excluded.server_sha256,
                                size = excluded.size,
                                sync_state = 'pending_local_sync'
                            """,
                            (project_id, change.relative_path,
                             change.baseline_sha256, change.server_sha256,
                             change.size),
                        )
                conn.execute(
                    "UPDATE projects SET sync_revision = ? WHERE id = ?",
                    (new_revision, project_id),
                )

            # 排序
            changes.sort(key=lambda c: c.relative_path)

        return new_revision, changes

    # ---- 文件下载 ----

    def download_project_change(
        self,
        user_id: str,
        project_id: str,
        relative_path: str,
        expected_server_sha256: str,
    ) -> ProjectFileDownload:
        """下载项目文件内容。"""
        _validate_user_id(user_id)
        _validate_project_id(project_id)
        _validate_relative_path(relative_path)
        _validate_sha256(expected_server_sha256)

        files_dir = self.get_files_dir(user_id, project_id)
        disk_path = files_dir / relative_path

        try:
            disk_path.resolve().relative_to(files_dir)
        except (ValueError, RuntimeError):
            raise ProjectFileValidationError("文件路径越界")

        if not disk_path.exists() or disk_path.is_symlink():
            raise ProjectStorageError("项目存储不可用")

        content = disk_path.read_bytes()
        actual_hash = _sha256_hex(content)

        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT baseline_sha256, server_sha256
                FROM project_files
                WHERE project_id = ? AND relative_path = ?
                """,
                (project_id, relative_path),
            ).fetchone()
            if row is None:
                raise ProjectStorageError("项目存储不可用")

            if actual_hash != expected_server_sha256:
                raise ProjectFileIntegrityError("文件 SHA-256 不匹配")

        return ProjectFileDownload(
            relative_path=relative_path,
            content=content,
            baseline_sha256=row["baseline_sha256"],
            server_sha256=actual_hash,
            size=len(content),
        )

    # ---- 同步确认 ----

    def ack_project_sync(
        self,
        user_id: str,
        project_id: str,
        entries: list[ProjectSyncAckEntry],
    ) -> ProjectSyncAckResult:
        """确认同步：baseline=server, sync_state=synced。"""
        _validate_user_id(user_id)
        _validate_project_id(project_id)

        # 先校验所有 entries
        seen_paths = set()
        for entry in entries:
            _validate_relative_path(entry.relative_path)
            if entry.server_sha256 is not None:
                _validate_sha256(entry.server_sha256)
            if entry.relative_path in seen_paths:
                raise ProjectFileValidationError("ack entries 包含重复路径")
            seen_paths.add(entry.relative_path)

        with self._lock, self._connect() as conn:
            proj = conn.execute(
                "SELECT sync_revision FROM projects WHERE id = ? AND user_id = ?",
                (project_id, user_id),
            ).fetchone()
            if proj is None:
                raise ProjectStorageError("项目存储不可用")

            # 校验所有 entries 对应的 DB 记录
            for entry in entries:
                row = conn.execute(
                    """
                    SELECT sync_state, server_sha256
                    FROM project_files
                    WHERE project_id = ? AND relative_path = ?
                    """,
                    (project_id, entry.relative_path),
                ).fetchone()
                if row is None:
                    raise ProjectSyncConflictError(
                        f"文件 {entry.relative_path} 不存在"
                    )
                if entry.server_sha256 is not None:
                    if row["server_sha256"] != entry.server_sha256:
                        raise ProjectFileIntegrityError(
                            f"文件 {entry.relative_path} 的 SHA-256 不匹配"
                        )

            # 全部校验通过后，执行更新
            new_revision = proj["sync_revision"]
            for entry in entries:
                if entry.server_sha256 is None:
                    # 删除标记
                    conn.execute(
                        "DELETE FROM project_files WHERE project_id = ? AND relative_path = ?",
                        (project_id, entry.relative_path),
                    )
                else:
                    conn.execute(
                        """
                        UPDATE project_files
                        SET baseline_sha256 = server_sha256,
                            sync_state = 'synced'
                        WHERE project_id = ? AND relative_path = ?
                        """,
                        (project_id, entry.relative_path),
                    )

            new_revision = proj["sync_revision"] + 1
            conn.execute(
                "UPDATE projects SET sync_revision = ? WHERE id = ?",
                (new_revision, project_id),
            )

        return ProjectSyncAckResult(
            project_id=project_id,
            sync_revision=new_revision,
            acked=sorted(seen_paths),
        )

    # ---- 辅助 ----

    @staticmethod
    def _row_to_project(row: sqlite3.Row) -> ProjectRecord:
        return ProjectRecord(
            id=row["id"],
            user_id=row["user_id"],
            name=row["name"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            sync_status=row["sync_status"],
            sync_revision=row["sync_revision"],
        )

    def _cleanup_quarantine(self) -> None:
        """构造时清理隔离区残留。"""
        try:
            if not self.projects_root.exists():
                return
            for user_dir in self.projects_root.iterdir():
                if not user_dir.is_dir() or user_dir.name.startswith("."):
                    continue
                quarantine = user_dir / ".deleting"
                if not quarantine.exists():
                    continue
                for item in quarantine.iterdir():
                    # 残留目录直接删除（DB 行已不在）
                    shutil.rmtree(item, ignore_errors=True)
        except OSError:
            pass


@lru_cache(maxsize=1)
def get_project_store() -> SQLiteProjectStore:
    """返回进程级单例。构造失败抛 ProjectStorageError。"""
    try:
        return SQLiteProjectStore(AUTH_DB_PATH, USER_PROJECTS_DIR)
    except Exception as exc:
        raise ProjectStorageError("项目存储不可用") from exc


__all__ = [
    "ProjectStorageError",
    "ProjectNameValidationError",
    "ProjectDeleteConfirmationError",
    "ProjectFileValidationError",
    "ProjectFileIntegrityError",
    "ProjectFileLimitError",
    "ProjectSyncConflictError",
    "ProjectRecord",
    "ProjectFileRecord",
    "ProjectDeleteResult",
    "ProjectSyncManifestEntry",
    "ProjectSyncFinishResult",
    "ProjectSyncAckEntry",
    "ProjectSyncAckResult",
    "ProjectChangeRecord",
    "ProjectFileDownload",
    "SQLiteProjectStore",
    "get_project_store",
]
