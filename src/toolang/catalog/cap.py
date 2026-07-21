"""Authored cap files and wired cap references."""

from __future__ import annotations

from contextlib import AbstractContextManager
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
import re
import tomllib
from typing import Any, Literal, cast

import frontmatter
import tomlkit

from toolang.common.files import atomic_write_text, file_write_lock

from ._frontmatter import normalize_meta
from .error import CatalogConflictError, CatalogNotFoundError

CapKind = Literal["psyche", "skill", "service", "prompt"]

CAP_KINDS: tuple[CapKind, ...] = ("psyche", "skill", "service", "prompt")
CAP_DIR_BY_KIND: dict[CapKind, str] = {
    "psyche": "psyches",
    "skill": "skills",
    "service": "services",
    "prompt": "prompts",
}
CAP_KIND_BY_DIR: dict[str, CapKind] = {
    directory: kind for kind, directory in CAP_DIR_BY_KIND.items()
}
CAP_DIRECTORY_NAMES = tuple(CAP_DIR_BY_KIND.values())
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


@dataclass(frozen=True, slots=True)
class CapRef:
    """One named wired cap reference."""

    kind: CapKind
    name: str
    ref: str

    def __post_init__(self) -> None:
        _validate_kind(self.kind)
        _validate_name(self.name)
        if not self.ref.strip():
            raise ValueError("cap ref cannot be empty")


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
                skill_dir = cap.path.parent
                for path in sorted(skill_dir.rglob("*"), reverse=True):
                    if path.is_file() or path.is_symlink():
                        path.unlink()
                    elif path.is_dir():
                        path.rmdir()
                skill_dir.rmdir()
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


class WiredCaps:
    """CRUD for named cap references in one explicitly supplied config file."""

    def __init__(self, config_path: Path) -> None:
        self.config_path = config_path

    @property
    def lock_path(self) -> Path:
        return self.config_path.with_name(f".{self.config_path.name}.lock")

    def write_lock(self) -> AbstractContextManager[None]:
        """Return the shared lock used by all wired-cap mutations."""

        return file_write_lock(self.lock_path)

    def list(self, *, kinds: set[CapKind] | None = None) -> tuple[CapRef, ...]:
        if not self.config_path.is_file():
            return ()
        return self.parse(
            self.config_path.read_text(encoding="utf-8"),
            kinds=kinds,
        )

    def parse(
        self,
        content: str,
        *,
        kinds: set[CapKind] | None = None,
    ) -> tuple[CapRef, ...]:
        data = cast(dict[str, object], tomllib.loads(content))
        refs: list[CapRef] = []
        for kind in CAP_KINDS:
            if kinds is not None and kind not in kinds:
                continue
            table = _kind_table(data, kind, create=False)
            if table is None:
                continue
            for name, item in sorted(table.items()):
                refs.append(CapRef(kind=kind, name=name, ref=_config_ref(item)))
        return tuple(refs)

    def get(self, kind: CapKind, name: str) -> CapRef | None:
        _validate_kind(kind)
        _validate_name(name)
        return next(
            (item for item in self.list(kinds={kind}) if item.name == name),
            None,
        )

    def create(self, cap: CapRef) -> CapRef:
        with self.write_lock():
            if self.get(cap.kind, cap.name) is not None:
                raise CatalogConflictError(
                    f"wired {cap.kind} already exists: {cap.name}"
                )
            self._write(cap)
            return cap

    def update(self, cap: CapRef) -> CapRef:
        with self.write_lock():
            if self.get(cap.kind, cap.name) is None:
                raise CatalogNotFoundError(f"wired {cap.kind} not found: {cap.name}")
            self._write(cap)
            return cap

    def upsert(self, cap: CapRef) -> CapRef:
        """Create or replace one wired capability atomically."""

        with self.write_lock():
            self._write(cap)
            return cap

    def remove(self, kind: CapKind, name: str) -> CapRef:
        with self.write_lock():
            cap = self.get(kind, name)
            if cap is None:
                raise CatalogNotFoundError(f"wired {kind} not found: {name}")
            document = _load_config_document(self.config_path)
            table = _document_kind_table(document, kind, create=False)
            if table is None or name not in table:
                raise CatalogNotFoundError(f"wired {kind} not found: {name}")
            del table[name]
            atomic_write_text(self.config_path, tomlkit.dumps(document))
            return cap

    def _write(self, cap: CapRef) -> None:
        document = _load_config_document(self.config_path)
        table = _document_kind_table(document, cap.kind, create=True)
        assert table is not None
        entry = table.get(cap.name)
        if isinstance(entry, Mapping):
            entry["ref"] = cap.ref
        else:
            encoded_ref = tomlkit.string(cap.ref).as_string()
            table[cap.name] = tomlkit.parse(f"value = {{ ref = {encoded_ref} }}\n")[
                "value"
            ]
        atomic_write_text(self.config_path, tomlkit.dumps(document))


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


def _kind_table(
    data: dict[str, object],
    kind: CapKind,
    *,
    create: bool,
) -> dict[str, object] | None:
    value = data.get(CAP_DIR_BY_KIND[kind])
    if isinstance(value, dict):
        return cast(dict[str, object], value)
    if value is not None:
        raise ValueError(f"invalid wired cap table: {CAP_DIR_BY_KIND[kind]}")
    if not create:
        return None
    table: dict[str, object] = {}
    data[CAP_DIR_BY_KIND[kind]] = table
    return table


def _config_ref(item: object) -> str:
    if isinstance(item, dict):
        ref = cast(dict[str, object], item).get("ref")
        if isinstance(ref, str) and ref.strip():
            return ref
    raise ValueError(f"invalid wired cap config entry: {item!r}")


def _load_config_document(path: Path) -> Any:
    content = path.read_text(encoding="utf-8") if path.is_file() else ""
    return tomlkit.parse(content)


def _document_kind_table(document: Any, kind: CapKind, *, create: bool) -> Any | None:
    key = CAP_DIR_BY_KIND[kind]
    value = document.get(key)
    if value is not None:
        if not isinstance(value, Mapping):
            raise ValueError(f"invalid wired cap table: {key}")
        return value
    if not create:
        return None
    value = tomlkit.table()
    document[key] = value
    return value
