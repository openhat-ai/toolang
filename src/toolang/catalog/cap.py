"""Authored capability files."""

from __future__ import annotations

from contextlib import AbstractContextManager
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
import re
import shutil
from uuid import uuid4

import frontmatter

from toolang.common.files import atomic_write_text, file_write_lock

from .common import normalize_meta
from .errors import CatalogConflictError, CatalogNotFoundError
from .types import CAP_DIR_BY_KIND, CAP_KINDS, CapKind

_SKILL_FIELDS = frozenset({"name", "description"})
_SERVICE_FIELDS = frozenset(
    {"name", "description", "transport", "protocol", "target", "headers", "env"}
)
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True, slots=True)
class CapFile:
    """One authored cap file."""

    path: Path | None
    content: str
    kind: CapKind
    meta: Mapping[str, object]
    body: str

    def __post_init__(self) -> None:
        _validate_cap(self)

    @property
    def name(self) -> str:
        return _required_meta_text(self.meta, "name")

    @classmethod
    def parse(
        cls,
        content: str,
        *,
        kind: CapKind,
        name: str,
        path: Path | None = None,
    ) -> CapFile:
        post = frontmatter.loads(content)
        meta = normalize_meta(post.metadata)
        authored_name = meta.get("name")
        if authored_name is not None and authored_name != name:
            raise ValueError(
                f"cap name does not match its path: {authored_name!r} != {name!r}"
            )
        meta["name"] = name
        cap = cls(
            path=path,
            content=content,
            kind=kind,
            meta=meta,
            body=post.content,
        )
        _validate_cap(cap)
        return cap


class AuthoredCaps:
    """CRUD for authored caps below one explicitly supplied directory."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    @property
    def lock_path(self) -> Path:
        return self.directory / ".authored-caps.lock"

    def write_lock(self) -> AbstractContextManager[None]:
        """Return the shared lock used by all authored-cap mutations."""

        return file_write_lock(self.lock_path)

    def list(self, *, kinds: set[CapKind] | None = None) -> tuple[CapFile, ...]:
        caps: list[CapFile] = []
        for kind in CAP_KINDS:
            if kinds is not None and kind not in kinds:
                continue
            directory = self.directory / CAP_DIR_BY_KIND[kind]
            if not directory.is_dir():
                continue
            paths = (
                sorted(directory.glob("*/SKILL.md"))
                if kind == "skill"
                else sorted(directory.glob("*.md"))
            )
            for path in paths:
                name = path.parent.name if kind == "skill" else path.stem
                caps.append(
                    CapFile.parse(
                        path.read_text(encoding="utf-8"),
                        kind=kind,
                        name=name,
                        path=path,
                    )
                )
        return tuple(sorted(caps, key=lambda cap: (cap.kind, cap.name)))

    def get(self, kind: CapKind, name: str) -> CapFile | None:
        path = self.path(kind, name)
        if not path.is_file():
            return None
        return CapFile.parse(
            path.read_text(encoding="utf-8"),
            kind=kind,
            name=name,
            path=path,
        )

    def create(self, cap: CapFile) -> CapFile:
        with self.write_lock():
            if self.get(cap.kind, cap.name) is not None:
                raise CatalogConflictError(
                    f"authored {cap.kind} already exists: {cap.name}"
                )
            return self._write(cap)

    def update(self, cap: CapFile) -> CapFile:
        with self.write_lock():
            if self.get(cap.kind, cap.name) is None:
                raise CatalogNotFoundError(f"authored {cap.kind} not found: {cap.name}")
            return self._write(cap)

    def upsert(self, cap: CapFile) -> CapFile:
        """Create or replace one authored capability atomically."""

        with self.write_lock():
            return self._write(cap)

    def remove(self, kind: CapKind, name: str) -> CapFile:
        with self.write_lock():
            cap = self.get(kind, name)
            if cap is None or cap.path is None:
                raise CatalogNotFoundError(f"authored {kind} not found: {name}")
            if kind == "skill":
                self._remove_skill_directory(cap.path.parent)
            else:
                cap.path.unlink()
            return cap

    def path(self, kind: CapKind, name: str) -> Path:
        _validate_kind(kind)
        _validate_name(name)
        root = self.directory / CAP_DIR_BY_KIND[kind]
        return root / name / "SKILL.md" if kind == "skill" else root / f"{name}.md"

    def _write(self, cap: CapFile) -> CapFile:
        _validate_cap(cap)
        path = self.path(cap.kind, cap.name)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, cap.content)
        return replace(cap, path=path)

    def _remove_skill_directory(self, directory: Path) -> None:
        trash = self.directory / ".runtime" / "authored-cap-trash"
        _prepare_directory(trash.parent, "authored cap runtime storage")
        _prepare_directory(trash, "authored cap trash")
        tombstone = trash / uuid4().hex
        directory.replace(tombstone)
        shutil.rmtree(tombstone, ignore_errors=True)


def _prepare_directory(path: Path, label: str) -> None:
    if path.is_symlink():
        raise ValueError(f"{label} must not be a symbolic link")
    if path.exists() and not path.is_dir():
        raise ValueError(f"{label} must be a directory")
    path.mkdir(parents=True, exist_ok=True)


def _validate_cap(cap: CapFile) -> None:
    _validate_kind(cap.kind)
    _validate_name(cap.name)
    if cap.kind == "skill":
        _require_allowed_fields(cap.kind, cap.meta, _SKILL_FIELDS)
        if not _optional_text(cap.meta, "description"):
            raise ValueError("skill description is required")
        if not cap.body.strip():
            raise ValueError("skill body is required")
    elif cap.kind == "service":
        _require_allowed_fields(cap.kind, cap.meta, _SERVICE_FIELDS)
        if not _optional_text(cap.meta, "description"):
            raise ValueError("service description is required")
        transport = cap.meta.get("transport") or cap.meta.get("protocol")
        if transport not in {"http", "stdio"}:
            raise ValueError("service transport must be http or stdio")
        if not _optional_text(cap.meta, "target"):
            raise ValueError("service target is required")
        headers = cap.meta.get("headers")
        if headers is not None and not _is_string_map(headers):
            raise ValueError("service headers must be a string map")
        env = cap.meta.get("env")
        if env is not None and not _is_env_names(env):
            raise ValueError("service env must list environment variable names")


def _validate_kind(kind: CapKind) -> None:
    if kind not in CAP_KINDS:
        raise ValueError(f"unsupported cap kind: {kind}")


def _validate_name(name: str) -> None:
    if not name.strip() or name in {".", ".."} or "/" in name or "\\" in name:
        raise ValueError(f"invalid cap name: {name!r}")


def _required_meta_text(meta: Mapping[str, object], key: str) -> str:
    value = _optional_text(meta, key)
    if value is None:
        raise ValueError(f"cap {key} is required")
    return value


def _optional_text(meta: Mapping[str, object], key: str) -> str | None:
    value = meta.get(key)
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()


def _require_allowed_fields(
    kind: CapKind,
    meta: Mapping[str, object],
    allowed: frozenset[str],
) -> None:
    unknown = sorted(set(meta) - set(allowed))
    if unknown:
        joined = ", ".join(repr(item) for item in unknown)
        raise ValueError(f"{kind} has unsupported frontmatter fields: {joined}")


def _is_string_map(value: object) -> bool:
    return isinstance(value, dict) and all(
        isinstance(key, str) and isinstance(item, str) for key, item in value.items()
    )


def _is_env_names(value: object) -> bool:
    if isinstance(value, str):
        items = [item.strip() for item in value.split(",")]
    elif isinstance(value, list | tuple):
        items = [item.strip() for item in value if isinstance(item, str)]
        if len(items) != len(value):
            return False
    else:
        return False
    return bool(items) and all(_ENV_NAME_RE.fullmatch(item) for item in items)
