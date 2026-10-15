"""Package-neutral filesystem mutation helpers."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from collections.abc import Mapping
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from typing import BinaryIO, TypeVar
from uuid import uuid4

_T = TypeVar("_T")


class _LockState:
    def __init__(self) -> None:
        self.mutex = threading.RLock()
        self.depth = 0
        self.handle: BinaryIO | None = None
        self.shared = False


_LOCK_STATES: dict[Path, _LockState] = {}
_LOCK_STATES_MUTEX = threading.Lock()


def file_lock_path(path: Path) -> Path:
    """Return the lock name shared by writers of an authored target."""
    return path.with_name(f"{path.name}.lock")


@contextmanager
def file_write_lock(
    path: Path,
    *,
    inherit_owner: bool = False,
    on_wait: Callable[[], None] | None = None,
    create: bool = True,
    blocking: bool = True,
    shared_owner: Mapping[str, object] | None = None,
    owner_dead: Callable[[dict], bool] | None = None,
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
        if not blocking:
            raise BlockingIOError(f"lock is busy: {path}")
        waiting()
        state.mutex.acquire()
    try:
        if state.depth == 0:
            if create:
                _prepare_directory(path.parent, inherit_owner=inherit_owner)
            state.shared = shared_owner is not None
            state.handle = (
                _claim_file(
                    path,
                    shared_owner,
                    owner_dead,
                    blocking=blocking,
                    inherit_owner=inherit_owner,
                    on_wait=waiting,
                )
                if shared_owner is not None
                else path.open("a+b" if create else "rb")
            )
            try:
                if inherit_owner:
                    _inherit_owner(state.handle.fileno(), path.parent)
                try:
                    fcntl.flock(state.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    if not blocking:
                        raise
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
                if state.shared:
                    _unlink_claim(path, state.handle)
                fcntl.flock(state.handle.fileno(), fcntl.LOCK_UN)
                state.handle.close()
                state.handle = None
    finally:
        state.mutex.release()


def _unlink_claim(path: Path, stream: BinaryIO) -> None:
    """Native flock serializes reapers; never unlink a replacement claim."""
    try:
        with file_read_snapshot(path) as current_stream:
            current = os.fstat(current_stream.fileno())
        owned = os.fstat(stream.fileno())
        if (current.st_dev, current.st_ino) == (owned.st_dev, owned.st_ino):
            path.unlink()
    except FileNotFoundError:
        pass


def _claim_file(
    path: Path,
    owner: Mapping[str, object],
    owner_dead: Callable[[dict], bool] | None,
    *,
    blocking: bool,
    inherit_owner: bool,
    on_wait: Callable[[], None],
) -> BinaryIO:
    """Publish a complete lock owner with an atomic hard link on shared mounts.

    flock alone cannot coordinate a host kernel with Docker Desktop's guest
    kernel. It still serializes stale-claim reapers within each native kernel;
    the callback must authorize reaping only in an owner's verifiable namespace.
    """
    descriptor, candidate = tempfile.mkstemp(dir=path.parent, prefix=".toolang-lock-")
    stream = os.fdopen(descriptor, "w+b")
    try:
        if inherit_owner:
            _inherit_owner(descriptor, path.parent)
        stream.write(json.dumps(dict(owner)).encode())
        stream.flush()
        os.fsync(descriptor)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        while True:
            try:
                os.link(candidate, path)
                return stream
            except FileExistsError:
                if path.is_symlink():
                    raise ValueError(f"shared lock must not be a symlink: {path}")
                reaped = (
                    reap_file_lock(path, owner_dead)
                    if owner_dead is not None
                    else False
                )
                if reaped:
                    continue
                if not blocking:
                    raise BlockingIOError(f"lock is busy: {path}")
                on_wait()
                time.sleep(0.02)
    except BaseException:
        stream.close()
        raise
    finally:
        Path(candidate).unlink(missing_ok=True)


def reap_file_lock(path: Path, owner_dead: Callable[[dict], bool]) -> bool:
    """Remove a proven dead claim while serializing native-namespace reapers."""
    try:
        with file_read_snapshot(path) as previous:
            try:
                fcntl.flock(previous.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return False
            content = previous.read()
            payload = json.loads(content) if content.strip() else {}
            if not isinstance(payload, dict):
                raise ValueError(f"invalid shared lock owner: {path}")
            if owner_dead(payload):
                _unlink_claim(path, previous)
                return True
    except FileNotFoundError:
        return True
    return False


@contextmanager
def file_read_snapshot(path: Path) -> Iterator[BinaryIO]:
    """Read an immutable inode through a fresh name on a shared mount.

    Docker Desktop can cache the replaced canonical name's size or absence.
    Creating a hard link resolves its current inode through the shared filesystem
    and avoids mixing fresh contents with stale attributes. The link is temporary.
    """
    if path.is_dir():
        raise IsADirectoryError(f"snapshot target is a directory: {path}")
    snapshot = path.with_name(f".toolang-read-{uuid4().hex}")
    os.link(path, snapshot)
    try:
        with snapshot.open("rb") as stream:
            yield stream
    finally:
        snapshot.unlink(missing_ok=True)


class EditLock:
    """One persistent edit guard beneath an authored scope's .state directory."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def _validate(self) -> None:
        # Validate the owning directory, .state, and root/home without resolving
        # a symlink into another scope. Missing directories are valid for reads.
        for directory in reversed(self.path.parents[:3]):
            if directory.is_symlink() or (
                directory.exists() and not directory.is_dir()
            ):
                raise OSError(f"edit lock ancestor must be a directory: {directory}")
        if self.path.is_symlink() or (self.path.exists() and not self.path.is_file()):
            raise OSError(f"edit lock must be a regular file: {self.path}")

    @contextmanager
    def write(self) -> Iterator[None]:
        self._validate()
        with file_write_lock(self.path, inherit_owner=True):
            yield

    def read(self, read: Callable[[], _T]) -> _T:
        """Read consistently without creating a guard or its directories."""
        self._validate()
        if not self.path.exists():
            try:
                result = read()
            except Exception as exc:
                # Configuration readers may wrap a disappearing source's I/O
                # failure with a domain error. Preserve unrelated failures.
                if not isinstance(exc, OSError) and not isinstance(
                    exc.__cause__, OSError
                ):
                    raise
                self._validate()
                if not self.path.exists():
                    raise
            else:
                self._validate()
                if not self.path.exists():
                    return result
        with file_write_lock(self.path, create=False):
            return read()


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
