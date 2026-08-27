"""Catalog-owned configuration entries and mutation."""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
import tomllib
from typing import Any, cast

import tomlkit

from toolang.common.files import atomic_write_text, file_write_lock

from .cap import _validate_kind, _validate_name
from .errors import CatalogConflictError, CatalogNotFoundError
from .types import CAP_DIR_BY_KIND, CAP_KINDS, CapKind


@dataclass(frozen=True, slots=True)
class CapRef:
    """One named configured capability reference."""

    kind: CapKind
    name: str
    ref: str

    def __post_init__(self) -> None:
        _validate_kind(self.kind)
        _validate_name(self.name)
        if not self.ref.strip():
            raise ValueError("cap ref cannot be empty")


class ConfiguredCaps:
    """CRUD for capability references in one explicitly supplied config file."""

    def __init__(self, config_path: Path) -> None:
        self.config_path = config_path

    @property
    def lock_path(self) -> Path:
        return self.config_path.with_name(f".{self.config_path.name}.lock")

    def write_lock(self) -> AbstractContextManager[None]:
        """Return the lock shared by configured-cap mutations."""

        return file_write_lock(self.lock_path)

    def list(self, *, kinds: set[CapKind] | None = None) -> tuple[CapRef, ...]:
        if not self.config_path.is_file():
            return ()
        return self.parse(self.config_path.read_text(encoding="utf-8"), kinds=kinds)

    @staticmethod
    def parse(
        content: str,
        *,
        kinds: set[CapKind] | None = None,
    ) -> tuple[CapRef, ...]:
        data = cast(dict[str, object], tomllib.loads(content))
        refs: list[CapRef] = []
        for kind in CAP_KINDS:
            if kinds is not None and kind not in kinds:
                continue
            table = _kind_table(data, kind)
            if table is None:
                continue
            refs.extend(
                CapRef(kind=kind, name=name, ref=_config_ref(item))
                for name, item in sorted(table.items())
            )
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
                    f"configured {cap.kind} already exists: {cap.name}"
                )
            self._write(cap)
            return cap

    def update(self, cap: CapRef) -> CapRef:
        with self.write_lock():
            if self.get(cap.kind, cap.name) is None:
                raise CatalogNotFoundError(
                    f"configured {cap.kind} not found: {cap.name}"
                )
            self._write(cap)
            return cap

    def upsert(self, cap: CapRef) -> CapRef:
        """Create or replace one configured capability atomically."""

        with self.write_lock():
            self._write(cap)
            return cap

    def remove(self, kind: CapKind, name: str) -> CapRef:
        with self.write_lock():
            cap = self.get(kind, name)
            if cap is None:
                raise CatalogNotFoundError(f"configured {kind} not found: {name}")
            document = _load_document(self.config_path)
            table = _document_kind_table(document, kind, create=False)
            if table is None or name not in table:
                raise CatalogNotFoundError(f"configured {kind} not found: {name}")
            del table[name]
            atomic_write_text(self.config_path, tomlkit.dumps(document))
            return cap

    def _write(self, cap: CapRef) -> None:
        document = _load_document(self.config_path)
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


def _kind_table(data: dict[str, object], kind: CapKind) -> dict[str, object] | None:
    value = data.get(CAP_DIR_BY_KIND[kind])
    if isinstance(value, dict):
        return cast(dict[str, object], value)
    if value is not None:
        raise ValueError(f"invalid configured cap table: {CAP_DIR_BY_KIND[kind]}")
    return None


def _config_ref(item: object) -> str:
    if isinstance(item, dict):
        ref = cast(dict[str, object], item).get("ref")
        if isinstance(ref, str) and ref.strip():
            return ref
    raise ValueError(f"invalid configured cap config entry: {item!r}")


def _load_document(path: Path) -> Any:
    content = path.read_text(encoding="utf-8") if path.is_file() else ""
    return tomlkit.parse(content)


def _document_kind_table(document: Any, kind: CapKind, *, create: bool) -> Any | None:
    key = CAP_DIR_BY_KIND[kind]
    value = document.get(key)
    if value is not None:
        if not isinstance(value, Mapping):
            raise ValueError(f"invalid configured cap table: {key}")
        return value
    if not create:
        return None
    value = tomlkit.table()
    document[key] = value
    return value
