"""Safe current-agent flow source authoring."""

from __future__ import annotations

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
    read_authored_source,
)
from toolang.state.state import flow_module_name


@dataclass(frozen=True, slots=True)
class AuthoredFlow:
    """One direct home flow source."""

    key: str
    path: Path
    source: str
    digest: str
    size: int


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
        directory = self._checked_directory(create=False)
        if directory is None:
            return ()
        entries: list[AuthoredFlow] = []
        for path in sorted(directory.iterdir(), key=lambda item: item.name):
            if path.suffix != ".too":
                continue
            if path.is_symlink() or not path.is_file():
                raise ValueError(f"flow source must be a regular file: {path.name}")
            entries.append(self._load(path.stem))
        return tuple(entries)

    def get(self, key: str) -> AuthoredFlow | None:
        path = self._exact_path(key)
        if path is None:
            return None
        self._require_regular(path)
        return self._load(key)

    def create(self, key: str, source: str) -> AuthoredFlow:
        with file_write_lock(self.lock_path):
            path = self.path(key, create_directory=True)
            collision = self._casefold_collision(key)
            if collision is not None:
                raise CatalogConflictError(f"authored flow already exists: {key}")
            self._validate_candidate(key, source)
            atomic_write_text(path, source)
            return self._load(key)

    def update(
        self,
        key: str,
        source: str,
        *,
        if_digest: str | None,
    ) -> tuple[AuthoredFlow, bool]:
        with file_write_lock(self.lock_path):
            current = self.get(key)
            if current is None:
                raise CatalogNotFoundError(f"authored flow not found: {key}")
            _check_digest(current.digest, if_digest, kind="flow", key=key)
            self._validate_candidate(key, source)
            if current.source == source:
                return current, False
            atomic_write_text(current.path, source)
            return self._load(key), True

    def delete(self, key: str, *, if_digest: str | None) -> AuthoredFlow:
        with file_write_lock(self.lock_path):
            current = self.get(key)
            if current is None:
                raise CatalogNotFoundError(f"authored flow not found: {key}")
            _check_digest(current.digest, if_digest, kind="flow", key=key)
            current.path.unlink()
            return current

    def path(self, key: str, *, create_directory: bool = False) -> Path:
        flow_module_name(f"flows/{key}.too")
        directory = self._checked_directory(create=create_directory)
        return (directory or self.directory) / f"{key}.too"

    def _checked_directory(self, *, create: bool) -> Path | None:
        directory = self.directory
        if directory.is_symlink():
            raise ValueError("flow source directory must not be a symbolic link")
        if directory.exists():
            if not directory.is_dir():
                raise ValueError("flow source path must be a directory")
            return directory
        if create:
            directory.mkdir(parents=True)
            return directory
        return None

    def _load(self, key: str) -> AuthoredFlow:
        path = self.path(key)
        self._require_regular(path)
        content = path.read_bytes()
        try:
            source = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"flow source is not valid UTF-8: {key}") from exc
        return AuthoredFlow(
            key=key,
            path=path,
            source=source,
            digest=sha256(content).hexdigest(),
            size=len(content),
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
            raise ValueError(f"flow source must be a regular file: {path.name}")

    def _validate_candidate(self, key: str, source: str) -> None:
        snapshot = read_authored_source(self.layout.root, self.layout.name)
        relative = self.path(key).relative_to(self.layout.root).as_posix()
        encoded = source.encode("utf-8")
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
