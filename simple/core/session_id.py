"""Canonical validation for session identifiers used as storage keys."""

import re


_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


class InvalidSessionIdError(ValueError):
    """Raised when a session ID cannot safely be used as an identity key."""


def validate_session_id(session_id: str) -> str:
    """Return a valid session ID unchanged; never sanitize or remap it."""
    if not isinstance(session_id, str) or not _SESSION_ID_RE.fullmatch(session_id):
        raise InvalidSessionIdError(
            "session_id 只能包含字母、数字、点、下划线和连字符，长度为 1-128"
        )
    if session_id in (".", ".."):
        raise InvalidSessionIdError("session_id 不能为 . 或 ..")
    return session_id
