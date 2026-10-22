"""Link-safe access to visiting homes beneath the shared temporary namespace."""

from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
import json
import os
from pathlib import Path
import stat

from toolang.common import layout as paths
from toolang.common.layout import AgentLayout
from toolang.common.files import file_write_lock
from toolang.common.place_codes import root_code


def verify_root_namespace(
    namespace: Path, toolang_root: Path, *, create: bool = False
) -> None:
    """Verify the full root behind a namespace hash without following links."""
    expected = {"root": str(toolang_root.expanduser().resolve())}
    if namespace.name != root_code(expected["root"]):
        raise ValueError("namespace does not match its Toolang root")
    if create:
        namespace.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory = os.open(namespace, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        _validate_directory(namespace, os.fstat(directory))
        flags = os.O_RDONLY | os.O_NOFOLLOW
        if create:
            if not (namespace / "root.json").exists() and any(namespace.iterdir()):
                raise ValueError(
                    "unbound root namespace already contains retained data"
                )
            try:
                descriptor = os.open(
                    "root.json",
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=directory,
                )
            except FileExistsError:
                pass
            else:
                with os.fdopen(descriptor, "w") as output:
                    json.dump(expected, output)
        descriptor = os.open("root.json", flags, dir_fd=directory)
        with os.fdopen(descriptor) as source:
            info = os.fstat(source.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600
            ):
                raise ValueError("unsafe namespace ownership record")
            if json.load(source) != expected:
                raise ValueError("root code collision: conflicting Toolang root")
    finally:
        os.close(directory)


def verify_home_paths(place: Path, name: str) -> None:
    """Require owned directories and ordinary retained data files without links."""
    home = place / "agents" / name
    for path in (place, place / "agents", home, home / ".runtime"):
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            if os.fstat(descriptor).st_uid != os.geteuid():
                raise ValueError(f"instance directory has a different owner: {path}")
        finally:
            os.close(descriptor)
    for path in (home / ".runtime/status.json", home / ".runtime/runs.db"):
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            continue
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
                raise ValueError(f"invalid instance data file: {path}")
        finally:
            os.close(descriptor)


@dataclass
class VisitingPaths:
    created: bool
    directories: list[tuple[Path, int]]

    def recheck(self) -> None:
        """Do not use a path replaced while waiting for its write guard."""
        for path, descriptor in self.directories:
            current = path.lstat()
            opened = os.fstat(descriptor)
            if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
                raise ValueError(f"visiting directory changed: {path}")
            _validate_directory(path, current, shared=path == paths.VISITING_BASE)


def _validate_directory(
    path: Path, info: os.stat_result, *, shared: bool = False
) -> None:
    mode = stat.S_IMODE(info.st_mode)
    private = info.st_uid == os.geteuid() and mode == 0o700
    shared_parent = shared and info.st_uid == 0 and mode == 0o1777
    if not stat.S_ISDIR(info.st_mode) or not (private or shared_parent):
        raise ValueError(f"unsafe visiting directory owner or mode: {path}")


@contextmanager
def _private_guard(path: Path, parent_fd: int) -> Iterator[None]:
    try:
        descriptor = os.open(
            path.name,
            os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=parent_fd,
        )
    except FileExistsError:
        descriptor = os.open(path.name, os.O_RDWR | os.O_NOFOLLOW, dir_fd=parent_fd)
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise ValueError(f"unsafe visiting guard: {path}")
        with file_write_lock(path, create=False):
            current = path.lstat()
            if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
                raise ValueError(f"visiting guard changed: {path}")
            yield
    finally:
        os.close(descriptor)


@contextmanager
def visiting_paths(layout: AgentLayout) -> Iterator[VisitingPaths]:
    """Pin every untrusted directory with O_NOFOLLOW and directory-relative I/O."""
    parent = paths.VISITING_BASE
    if layout.root is None or layout.place_code is None:
        raise ValueError("visiting agent root requires its Toolang root")
    namespace = parent / root_code(str(layout.root))
    if layout.place != namespace / layout.place_code:
        raise ValueError("visiting agent root does not match its instance identity")
    descriptors: list[tuple[Path, int]] = []
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW

    def open_directory(path: Path, parent_fd: int, *, create: bool) -> tuple[int, bool]:
        created = False
        if create:
            try:
                os.mkdir(path.name, mode=0o700, dir_fd=parent_fd)
                created = True
            except FileExistsError:
                pass
        descriptor = os.open(path.name, flags, dir_fd=parent_fd)
        descriptors.append((path, descriptor))
        _validate_directory(path, os.fstat(descriptor), shared=path == parent)
        return descriptor, created

    # Only the platform /tmp alias is resolved. Never resolve toolang or its
    # descendants through an existing link before opening them.
    base = os.open(parent.parent, flags)
    locks = ExitStack()
    try:
        parent_fd, _ = open_directory(parent, base, create=True)
        # Serialize namespace ownership publication briefly; independent homes use
        # separate guards while their source inputs are prepared.
        with _private_guard(parent / f".{namespace.name}.lock", parent_fd):
            namespace_fd, _ = open_directory(namespace, parent_fd, create=True)
            verify_root_namespace(namespace, layout.root, create=True)
        locks.enter_context(
            _private_guard(namespace / f".{layout.place_code}.lock", namespace_fd)
        )
        place_fd, created = open_directory(layout.place, namespace_fd, create=True)
        agents_fd, _ = open_directory(layout.place / "agents", place_fd, create=created)
        home_fd, _ = open_directory(layout.home, agents_fd, create=created)
        open_directory(layout.runtime, home_fd, create=created)
        state_fd, _ = open_directory(layout.home / ".state", home_fd, create=created)
        home_state_fd, _ = open_directory(layout.home_state, state_fd, create=created)
        if created:
            descriptor = os.open(
                layout.home_edit_lock.name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=home_state_fd,
            )
            os.close(descriptor)
        for path in (
            layout.home_edit_lock,
            layout.runtime_status,
            layout.program,
            layout.home / "agent.source.json",
        ):
            try:
                info = path.lstat()
            except FileNotFoundError:
                continue
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or (
                    path != layout.home_edit_lock
                    and stat.S_IMODE(info.st_mode) != 0o600
                )
            ):
                raise ValueError(f"unsafe visiting file owner or mode: {path}")
        pinned = VisitingPaths(created, descriptors)
        pinned.recheck()
        yield pinned
    finally:
        locks.close()
        for _, descriptor in reversed(descriptors):
            os.close(descriptor)
        os.close(base)
