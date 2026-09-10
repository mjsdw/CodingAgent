"""Utilities for replacing text files without exposing partial writes."""

import os
import stat
import tempfile
from pathlib import Path


def atomic_write_text(filepath: str | Path, content: str) -> None:
    """Write UTF-8 text to *filepath* using an atomic same-directory replace."""
    target = Path(filepath)
    target.parent.mkdir(parents=True, exist_ok=True)

    fd: int | None = None
    temp_path: Path | None = None
    try:
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{target.name}.",
            suffix=".tmp",
            dir=str(target.parent),
        )
        temp_path = Path(temp_name)

        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            fd = None
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())

        if target.exists():
            try:
                mode = stat.S_IMODE(target.stat().st_mode)
                os.chmod(temp_path, mode)
            except OSError:
                # Permission preservation is best-effort; replacement safety is not.
                pass

        os.replace(temp_path, target)
        temp_path = None
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
