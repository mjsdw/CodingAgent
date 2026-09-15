"""Authentication validation, token, hashing, and rate-limit helpers."""

from __future__ import annotations

import hashlib
import re
import secrets
import threading
import time
from collections import deque
from collections.abc import Callable

import bcrypt


AUTH_COOKIE_NAME = "codeagent_session"
CSRF_HEADER_NAME = "X-CSRF-Token"

_USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")


class CredentialValidationError(ValueError):
    """Raised when a username or password does not meet the auth policy."""


def validate_username(value: str) -> tuple[str, str]:
    """Return the display and normalized forms of a valid username."""
    if not isinstance(value, str):
        raise CredentialValidationError("用户名格式无效")

    display_name = value.strip()
    if not _USERNAME_PATTERN.fullmatch(display_name):
        raise CredentialValidationError(
            "用户名必须为 3-32 位，只能包含字母、数字、点、下划线和短横线"
        )

    return display_name, display_name.lower()


def validate_password(value: str) -> str:
    """Validate bcrypt-compatible password length in UTF-8 bytes."""
    if not isinstance(value, str):
        raise CredentialValidationError("密码格式无效")

    byte_length = len(value.encode("utf-8"))
    if byte_length < 10 or byte_length > 72:
        raise CredentialValidationError("密码长度必须为 10-72 个 UTF-8 字节")

    return value


def hash_password(password: str, rounds: int = 12) -> str:
    """Hash a validated password with bcrypt."""
    validated = validate_password(password)
    password_hash = bcrypt.hashpw(
        validated.encode("utf-8"), bcrypt.gensalt(rounds=rounds)
    )
    return password_hash.decode("ascii")


def verify_password(password: str, password_hash: str) -> bool:
    """Return whether a password matches a bcrypt hash."""
    if not isinstance(password, str) or not isinstance(password_hash, str):
        return False

    try:
        return bcrypt.checkpw(
            password.encode("utf-8"), password_hash.encode("ascii")
        )
    except (TypeError, ValueError, UnicodeError):
        return False


def generate_session_token() -> str:
    """Generate a cryptographically secure session token."""
    return secrets.token_urlsafe(32)


def generate_csrf_token() -> str:
    """Generate a cryptographically secure CSRF token."""
    return secrets.token_urlsafe(32)


def hash_session_token(token: str) -> str:
    """Return the SHA-256 digest stored for a session token."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class AuthRateLimiter:
    """Thread-safe, in-memory sliding-window limiter for auth attempts."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._attempts: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def consume(self, key: str, limit: int, window_seconds: float) -> bool:
        """Consume one attempt, returning False when the window is full."""
        if limit <= 0 or window_seconds <= 0:
            raise ValueError("limit and window_seconds must be positive")

        now = self._clock()
        cutoff = now - window_seconds

        with self._lock:
            attempts = self._attempts.setdefault(key, deque())
            while attempts and attempts[0] <= cutoff:
                attempts.popleft()

            if len(attempts) >= limit:
                return False

            attempts.append(now)
            return True

    def clear(self, key: str) -> None:
        """Clear all recorded attempts for a key."""
        with self._lock:
            self._attempts.pop(key, None)


__all__ = [
    "AUTH_COOKIE_NAME",
    "CSRF_HEADER_NAME",
    "CredentialValidationError",
    "validate_username",
    "validate_password",
    "hash_password",
    "verify_password",
    "generate_session_token",
    "generate_csrf_token",
    "hash_session_token",
    "AuthRateLimiter",
]
