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
def file_write_lock(path: Path, *, inherit_owner: bool = False) -> Iterator[None]:
    """Lock across processes, optionally retaining the parent owner as root."""

    key = path.resolve(strict=False)
    with _LOCK_STATES_MUTEX:
        state = _LOCK_STATES.setdefault(key, _LockState())
    with state.mutex:
        if state.depth == 0:
            _prepare_directory(path.parent, inherit_owner=inherit_owner)
            state.handle = path.open("a+b")
            try:
                if inherit_owner:
                    _inherit_owner(state.handle.fileno(), path.parent)
                fcntl.flock(state.handle.fileno(), fcntl.LOCK_EX)
            except BaseException:
                state.handle.close()
                state.handle = None
                raise
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


def atomic_write_text(path: Path, content: str, *, inherit_owner: bool = False) -> None:
    """Replace UTF-8 text, preserving mode and optionally the parent owner."""

    _prepare_directory(path.parent, inherit_owner=inherit_owner)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            if inherit_owner:
                _inherit_owner(descriptor, path.parent)
            if path.exists():
                os.fchmod(descriptor, path.stat().st_mode)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def _prepare_directory(path: Path, *, inherit_owner: bool) -> None:
    """Let privileged writers retain the mounted parent's owner for new paths."""

    if not inherit_owner or os.geteuid() != 0:
        path.mkdir(parents=True, exist_ok=True)
    elif not path.is_dir():
        _prepare_directory(path.parent, inherit_owner=True)
        try:
            path.mkdir()
        except FileExistsError:
            if not path.is_dir():
                raise
        else:
            parent = path.parent.stat()
            os.chown(path, parent.st_uid, parent.st_gid)


def _inherit_owner(descriptor: int, directory: Path) -> None:
    """Keep a root container's writes accessible to the mounted directory owner."""

    if os.geteuid() == 0:
        parent = directory.stat()
        current = os.fstat(descriptor)
        if (current.st_uid, current.st_gid) != (parent.st_uid, parent.st_gid):
            os.fchown(descriptor, parent.st_uid, parent.st_gid)
