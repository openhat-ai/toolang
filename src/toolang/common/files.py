"""Package-neutral filesystem mutation helpers."""

from __future__ import annotations

from collections.abc import Callable, Iterator
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


def file_lock_path(path: Path) -> Path:
    """Return the lock name shared by writers of an authored target."""
    return path.with_name(f".{path.name}.lock")


@contextmanager
def file_write_lock(
    path: Path,
    *,
    inherit_owner: bool = False,
    on_wait: Callable[[], None] | None = None,
) -> Iterator[None]:
    """Lock across processes, optionally retaining the parent owner as root."""

    key = path.resolve(strict=False)
    with _LOCK_STATES_MUTEX:
        state = _LOCK_STATES.setdefault(key, _LockState())

    def waiting() -> None:
        if on_wait is not None:
            try:
                on_wait()
            except Exception:
                pass

    if not state.mutex.acquire(blocking=False):
        waiting()
        state.mutex.acquire()
    try:
        if state.depth == 0:
            _prepare_directory(path.parent, inherit_owner=inherit_owner)
            state.handle = path.open("a+b")
            try:
                if inherit_owner:
                    _inherit_owner(state.handle.fileno(), path.parent)
                try:
                    fcntl.flock(state.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    waiting()
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
    finally:
        state.mutex.release()


def atomic_write_text(path: Path, content: str, *, inherit_owner: bool = False) -> None:
    """Replace UTF-8 text, preserving mode and optionally the parent owner."""

    atomic_write_bytes(path, content.encode("utf-8"), inherit_owner=inherit_owner)


def atomic_write_bytes(
    path: Path, content: bytes, *, inherit_owner: bool = False
) -> None:
    """Replace exact bytes atomically, preserving the existing file mode."""
    _prepare_directory(path.parent, inherit_owner=inherit_owner)
    # A valid target name may already occupy the filesystem's full name limit.
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".toolang-")
    try:
        with os.fdopen(descriptor, "wb") as stream:
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
