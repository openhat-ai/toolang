"""Package-neutral filesystem mutation helpers."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import tempfile
import threading
from typing import BinaryIO


class _LockState:
    def __init__(self) -> None:
        self.mutex = threading.RLock()
        self.depth = 0
        self.handle: BinaryIO | None = None


_LOCK_STATES: dict[Path, _LockState] = {}
_LOCK_STATES_MUTEX = threading.Lock()


@contextmanager
def file_write_lock(path: Path) -> Iterator[None]:
    """Hold a reentrant inter-process exclusive lock backed by one file."""

    key = path.resolve(strict=False)
    with _LOCK_STATES_MUTEX:
        state = _LOCK_STATES.setdefault(key, _LockState())
    with state.mutex:
        if state.depth == 0:
            path.parent.mkdir(parents=True, exist_ok=True)
            state.handle = path.open("a+b")
            fcntl.flock(state.handle.fileno(), fcntl.LOCK_EX)
        state.depth += 1
        try:
            yield
        finally:
            state.depth -= 1
            if state.depth == 0:
                assert state.handle is not None
                fcntl.flock(state.handle.fileno(), fcntl.LOCK_UN)
                state.handle.close()
                state.handle = None


def atomic_write_text(path: Path, content: str) -> None:
    """Atomically replace one UTF-8 text file and preserve its mode."""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        if path.exists():
            os.fchmod(descriptor, path.stat().st_mode)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
