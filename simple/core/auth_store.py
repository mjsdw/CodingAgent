"""SQLite persistence for users, login sessions, and resource ownership."""

from __future__ import annotations

import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from config import AUTH_DB_PATH


AUTH_SCHEMA_VERSION = 2


_AUTH_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    username TEXT NOT NULL,
    normalized_username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS auth_sessions (
    token_hash TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    csrf_token TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    last_seen_at REAL NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS chat_sessions (
    session_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    created_at REAL NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    name TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    sync_status TEXT NOT NULL DEFAULT 'ready',
    sync_revision INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS project_files (
    project_id TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    baseline_sha256 TEXT NOT NULL,
    server_sha256 TEXT NOT NULL,
    size INTEGER NOT NULL,
    sync_state TEXT NOT NULL,
    PRIMARY KEY (project_id, relative_path),
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_auth_sessions_user_id
ON auth_sessions(user_id);

CREATE INDEX IF NOT EXISTS idx_auth_sessions_expires_at
ON auth_sessions(expires_at);

CREATE INDEX IF NOT EXISTS idx_chat_sessions_user_id
ON chat_sessions(user_id);

CREATE INDEX IF NOT EXISTS idx_projects_user_id
ON projects(user_id);
"""


@dataclass(frozen=True, slots=True)
class UserRecord:
    """Public user identity safe for application-level authorization."""

    id: str
    username: str
    is_active: bool
    created_at: float


@dataclass(frozen=True, slots=True)
class UserCredentialRecord:
    """Private credential record used only during login verification."""

    id: str
    username: str
    password_hash: str
    is_active: bool
    created_at: float


@dataclass(frozen=True, slots=True)
class AuthSessionRecord:
    """A resolved authentication session and its owning user."""

    token_hash: str
    csrf_token: str
    created_at: float
    expires_at: float
    last_seen_at: float
    user: UserRecord


class DuplicateUsernameError(ValueError):
    """Raised when a normalized username has already been registered."""


class SessionOwnershipError(ValueError):
    """Raised when a chat session belongs to a different user."""


class SQLiteAuthStore:
    """Own the versioned SQLite schema for identity and ownership data."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize_schema()

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

    def _initialize_schema(self) -> None:
        with self._lock, self._connect() as conn:
            current_version = conn.execute("PRAGMA user_version").fetchone()[0]
            if current_version > AUTH_SCHEMA_VERSION:
                raise RuntimeError(
                    "认证数据库版本高于当前程序支持版本: "
                    f"{current_version} > {AUTH_SCHEMA_VERSION}"
                )

            conn.executescript(_AUTH_SCHEMA)
            project_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(projects)")
            }
            if "sync_status" not in project_columns:
                conn.execute(
                    "ALTER TABLE projects "
                    "ADD COLUMN sync_status TEXT NOT NULL DEFAULT 'ready'"
                )
            if "sync_revision" not in project_columns:
                conn.execute(
                    "ALTER TABLE projects "
                    "ADD COLUMN sync_revision INTEGER NOT NULL DEFAULT 0"
                )

            conn.execute(f"PRAGMA user_version = {AUTH_SCHEMA_VERSION}")

    def create_user(
        self,
        username: str,
        normalized_username: str,
        password_hash: str,
        *,
        now: float | None = None,
    ) -> UserRecord:
        created_at = time.time() if now is None else now
        user = UserRecord(
            id=f"user-{uuid.uuid4().hex}",
            username=username,
            is_active=True,
            created_at=created_at,
        )

        try:
            with self._lock, self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO users
                        (id, username, normalized_username, password_hash,
                         is_active, created_at)
                    VALUES (?, ?, ?, ?, 1, ?)
                    """,
                    (
                        user.id,
                        user.username,
                        normalized_username,
                        password_hash,
                        user.created_at,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            if "users.normalized_username" in str(exc):
                raise DuplicateUsernameError("用户名已存在") from exc
            raise

        return user

    def get_user_credentials(
        self,
        normalized_username: str,
    ) -> UserCredentialRecord | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT id, username, password_hash, is_active, created_at
                FROM users
                WHERE normalized_username = ?
                """,
                (normalized_username,),
            ).fetchone()

        if row is None:
            return None
        return UserCredentialRecord(
            id=row["id"],
            username=row["username"],
            password_hash=row["password_hash"],
            is_active=bool(row["is_active"]),
            created_at=row["created_at"],
        )

    def create_auth_session(
        self,
        user_id: str,
        *,
        token_hash: str,
        csrf_token: str,
        now: float | None = None,
        expires_at: float,
    ) -> AuthSessionRecord:
        created_at = time.time() if now is None else now
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO auth_sessions
                    (token_hash, user_id, csrf_token, created_at,
                     expires_at, last_seen_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    token_hash,
                    user_id,
                    csrf_token,
                    created_at,
                    expires_at,
                    created_at,
                ),
            )

        created = self.get_auth_session(token_hash, now=created_at)
        if created is None:
            raise RuntimeError("登录会话创建失败")
        return created

    def get_auth_session(
        self,
        token_hash: str,
        *,
        now: float | None = None,
    ) -> AuthSessionRecord | None:
        checked_at = time.time() if now is None else now
        with self._lock, self._connect() as conn:
            expired = conn.execute(
                """
                DELETE FROM auth_sessions
                WHERE token_hash = ? AND expires_at <= ?
                """,
                (token_hash, checked_at),
            )
            if expired.rowcount > 0:
                return None

            row = conn.execute(
                """
                SELECT s.token_hash, s.csrf_token, s.created_at,
                       s.expires_at, s.last_seen_at,
                       u.id AS user_id, u.username, u.is_active,
                       u.created_at AS user_created_at
                FROM auth_sessions AS s
                JOIN users AS u ON u.id = s.user_id
                WHERE s.token_hash = ?
                """,
                (token_hash,),
            ).fetchone()
            if row is None:
                return None
            if not bool(row["is_active"]):
                conn.execute(
                    "DELETE FROM auth_sessions WHERE token_hash = ?",
                    (token_hash,),
                )
                return None

            conn.execute(
                "UPDATE auth_sessions SET last_seen_at = ? WHERE token_hash = ?",
                (checked_at, token_hash),
            )

        return AuthSessionRecord(
            token_hash=row["token_hash"],
            csrf_token=row["csrf_token"],
            created_at=row["created_at"],
            expires_at=row["expires_at"],
            last_seen_at=checked_at,
            user=UserRecord(
                id=row["user_id"],
                username=row["username"],
                is_active=bool(row["is_active"]),
                created_at=row["user_created_at"],
            ),
        )

    def revoke_auth_session(self, token_hash: str) -> bool:
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM auth_sessions WHERE token_hash = ?",
                (token_hash,),
            )
            return cursor.rowcount > 0

    def create_chat_session(
        self,
        user_id: str,
        session_id: str,
        *,
        now: float | None = None,
    ) -> bool:
        """Atomically create ownership or verify the existing owner."""
        created_at = time.time() if now is None else now
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO chat_sessions (session_id, user_id, created_at)
                VALUES (?, ?, ?)
                ON CONFLICT(session_id) DO NOTHING
                """,
                (session_id, user_id, created_at),
            )
            row = conn.execute(
                "SELECT user_id FROM chat_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()

            if row is None:
                raise RuntimeError("创建会话归属记录失败")
            if row["user_id"] != user_id:
                raise SessionOwnershipError("会话属于其他用户")
            return cursor.rowcount > 0

    def owns_chat_session(self, user_id: str, session_id: str) -> bool:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT 1
                FROM chat_sessions
                WHERE session_id = ? AND user_id = ?
                """,
                (session_id, user_id),
            ).fetchone()
        return row is not None

    def list_user_chat_session_ids(self, user_id: str) -> list[str]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT session_id
                FROM chat_sessions
                WHERE user_id = ?
                ORDER BY created_at DESC, session_id ASC
                """,
                (user_id,),
            ).fetchall()
        return [row["session_id"] for row in rows]

    def delete_chat_session(self, user_id: str, session_id: str) -> bool:
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                """
                DELETE FROM chat_sessions
                WHERE session_id = ? AND user_id = ?
                """,
                (session_id, user_id),
            )
            return cursor.rowcount > 0


@lru_cache(maxsize=1)
def get_auth_store() -> SQLiteAuthStore:
    """Return the process-wide initialized authentication store."""
    return SQLiteAuthStore(AUTH_DB_PATH)


__all__ = [
    "AUTH_SCHEMA_VERSION",
    "AuthSessionRecord",
    "DuplicateUsernameError",
    "SessionOwnershipError",
    "SQLiteAuthStore",
    "UserCredentialRecord",
    "UserRecord",
    "get_auth_store",
]
