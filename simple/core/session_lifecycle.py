"""Thread-safe lifecycle guards for session-scoped asynchronous work."""

import threading


class SessionDeletingError(RuntimeError):
    """Raised when work targets a deleting or superseded session lifecycle."""


_LOCK = threading.Lock()
_GENERATIONS: dict[str, int] = {}
_DELETING: set[str] = set()


def begin_request(session_id: str) -> int:
    """Capture the current generation, rejecting requests during deletion."""
    with _LOCK:
        if session_id in _DELETING:
            raise SessionDeletingError("会话正在删除，请稍后重试")
        return _GENERATIONS.get(session_id, 0)


def begin_delete(session_id: str) -> int:
    """Invalidate existing work and mark the session as deleting."""
    with _LOCK:
        if session_id in _DELETING:
            raise SessionDeletingError("会话正在删除，请稍后重试")
        generation = _GENERATIONS.get(session_id, 0) + 1
        _GENERATIONS[session_id] = generation
        _DELETING.add(session_id)
        return generation


def end_delete(session_id: str) -> None:
    """Allow new work after all deletion cleanup has finished."""
    with _LOCK:
        _DELETING.discard(session_id)


def require_current(session_id: str, generation: int) -> None:
    """Reject writes from work that predates deletion."""
    with _LOCK:
        current = _GENERATIONS.get(session_id, 0)
        if session_id in _DELETING or generation != current:
            raise SessionDeletingError("会话已被删除，已丢弃迟到结果")
