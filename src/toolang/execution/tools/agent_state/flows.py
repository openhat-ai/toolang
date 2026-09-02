"""Safe current-agent flow source authoring."""

from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from toolang.catalog.errors import CatalogConflictError, CatalogNotFoundError
from toolang.common.files import atomic_write_text, file_write_lock
from toolang.common.layout import AgentLayout
from toolang.state.prepare import validate_home_programs
from toolang.state.source import (
    SourceFile,
    SourceSnapshot,
    read_home_program_source,
)
from toolang.state.state import flow_module_name

from .storage import UnsafeAuthoringPathError, require_regular_file


@dataclass(frozen=True, slots=True)
class AuthoredFlow:
    """One direct home flow source."""

    key: str
    path: Path
    source: str
    digest: str
    size: int


@dataclass(frozen=True, slots=True)
class _StoredFlow:
    path: Path
    content: bytes
    digest: str


class AuthoredFlows:
    """CRUD for direct validated flow modules in one agent home."""

    def __init__(self, layout: AgentLayout) -> None:
        self.layout = layout

    @property
    def directory(self) -> Path:
        return self.layout.home / "flows"

    @property
    def lock_path(self) -> Path:
        return self.layout.home / ".authored-flows.lock"

    def list(self) -> tuple[AuthoredFlow, ...]:
        with self._lock():
            directory = self._checked_directory(create=False)
            if directory is None:
                return ()
            entries: list[AuthoredFlow] = []
            for path in sorted(directory.iterdir(), key=lambda item: item.name):
                if path.suffix != ".too":
                    continue
                self._require_regular(path)
                entries.append(self._load(path.stem))
            return tuple(entries)

    def get(self, key: str) -> AuthoredFlow | None:
        with self._lock():
            stored = self._stored(key)
            return None if stored is None else self._flow(key, stored)

    def create(self, key: str, source: str) -> AuthoredFlow:
        with self._lock():
            path = self.path(key, create_directory=True)
            collision = self._casefold_collision(key)
            if collision is not None:
                raise CatalogConflictError(f"authored flow already exists: {key}")
            encoded = source.encode("utf-8")
            self._validate_candidate(key, encoded)
            atomic_write_text(path, source)
            return self._flow_from_content(key, path, source, encoded)

    def update(
        self,
        key: str,
        source: str,
        *,
        if_digest: str | None,
    ) -> tuple[AuthoredFlow, bool]:
        with self._lock():
            current = self._stored(key)
            if current is None:
                raise CatalogNotFoundError(f"authored flow not found: {key}")
            _check_digest(current.digest, if_digest, kind="flow", key=key)
            encoded = source.encode("utf-8")
            self._validate_candidate(key, encoded)
            if current.content == encoded:
                return self._flow(key, current), False
            atomic_write_text(current.path, source)
            return self._flow_from_content(key, current.path, source, encoded), True

    def delete(self, key: str, *, if_digest: str | None) -> None:
        with self._lock():
            current = self._stored(key)
            if current is None:
                raise CatalogNotFoundError(f"authored flow not found: {key}")
            _check_digest(current.digest, if_digest, kind="flow", key=key)
            current.path.unlink()

    def path(self, key: str, *, create_directory: bool = False) -> Path:
        flow_module_name(f"flows/{key}.too")
        directory = self._checked_directory(create=create_directory)
        return (directory or self.directory) / f"{key}.too"

    def _checked_directory(self, *, create: bool) -> Path | None:
        directory = self.directory
        if directory.is_symlink():
            raise UnsafeAuthoringPathError(
                "flow source directory must not be a symbolic link"
            )
        if directory.exists():
            if not directory.is_dir():
                raise UnsafeAuthoringPathError("flow source path must be a directory")
            return directory
        if create:
            directory.mkdir(parents=True)
            return directory
        return None

    def _load(self, key: str) -> AuthoredFlow:
        stored = self._stored(key)
        if stored is None:
            raise CatalogNotFoundError(f"authored flow not found: {key}")
        return self._flow(key, stored)

    def _flow(self, key: str, stored: _StoredFlow) -> AuthoredFlow:
        try:
            source = stored.content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"flow source is not valid UTF-8: {key}") from exc
        return self._flow_from_content(key, stored.path, source, stored.content)

    def _flow_from_content(
        self,
        key: str,
        path: Path,
        source: str,
        content: bytes,
    ) -> AuthoredFlow:
        return AuthoredFlow(
            key=key,
            path=path,
            source=source,
            digest=sha256(content).hexdigest(),
            size=len(content),
        )

    def _stored(self, key: str) -> _StoredFlow | None:
        path = self._exact_path(key)
        if path is None:
            return None
        self._require_regular(path)
        content = path.read_bytes()
        return _StoredFlow(
            path=path,
            content=content,
            digest=sha256(content).hexdigest(),
        )

    def _casefold_collision(self, key: str) -> Path | None:
        directory = self._checked_directory(create=False)
        if directory is None:
            return None
        expected = key.casefold()
        return next(
            (
                path
                for path in directory.iterdir()
                if path.suffix == ".too" and path.stem.casefold() == expected
            ),
            None,
        )

    def _exact_path(self, key: str) -> Path | None:
        flow_module_name(f"flows/{key}.too")
        directory = self._checked_directory(create=False)
        if directory is None:
            return None
        expected = f"{key}.too"
        return next(
            (path for path in directory.iterdir() if path.name == expected),
            None,
        )

    def _require_regular(self, path: Path) -> None:
        if path.is_symlink() or not path.is_file():
            raise UnsafeAuthoringPathError(
                f"flow source must be a regular file: {path.name}"
            )

    def _lock(self) -> AbstractContextManager[None]:
        require_regular_file(self.lock_path, "flow lock")
        return file_write_lock(self.lock_path)

    def _validate_candidate(self, key: str, encoded: bytes) -> None:
        self._validate_source_storage()
        snapshot = read_home_program_source(self.layout.root, self.layout.name)
        relative = self.path(key).relative_to(self.layout.root).as_posix()
        candidate = SourceFile(
            path=self.path(key),
            relative_path=relative,
            category="program",
            origin="agent",
            content=encoded,
            digest=sha256(encoded).hexdigest(),
            size=len(encoded),
        )
        files = tuple(
            sorted(
                (
                    *(
                        item
                        for item in snapshot.files
                        if item.relative_path != relative
                    ),
                    candidate,
                ),
                key=lambda item: item.relative_path,
            )
        )
        validate_home_programs(
            SourceSnapshot(
                toolang_root=snapshot.toolang_root,
                agent_name=snapshot.agent_name,
                files=files,
            )
        )

    def _validate_source_storage(self) -> None:
        directory = self._checked_directory(create=False)
        if directory is None:
            return
        for path in directory.iterdir():
            if path.suffix == ".too":
                self._require_regular(path)


def _check_digest(
    current: str,
    expected: str | None,
    *,
    kind: str,
    key: str,
) -> None:
    if expected is not None and current != expected:
        raise DigestMismatchError(kind=kind, key=key, expected=expected, actual=current)


class DigestMismatchError(ValueError):
    """An authored resource changed since the caller observed it."""

    def __init__(
        self,
        *,
        kind: str,
        key: str,
        expected: str,
        actual: str,
    ) -> None:
        self.kind = kind
        self.key = key
        self.expected = expected
        self.actual = actual
        super().__init__(f"{kind} digest changed: {key}")
