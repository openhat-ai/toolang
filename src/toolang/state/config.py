"""Agent configuration for durable State and local publications."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import AbstractContextManager
import os
from pathlib import Path
import re
import tomllib
from typing import Any, cast
import unicodedata

import tomlkit

from toolang.catalog.types import CAP_DIR_BY_KIND, CAP_KINDS
from toolang.common.errors import ToolangError
from toolang.common.files import atomic_write_text, file_write_lock
from toolang.common.query import resolve_query_sentinels

CAP_ALLOW_FIELDS = tuple(f"{kind}s" for kind in CAP_KINDS)
_CAP_TABLES = tuple(CAP_DIR_BY_KIND[kind] for kind in CAP_KINDS)
_WORKSPACE_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_WORKSPACE_NAME_SEPARATOR_RE = re.compile(r"[^a-z0-9]+")
_LOWER_TO_UPPER_RE = re.compile(r"([a-z0-9])([A-Z])")
_ACRONYM_TO_WORD_RE = re.compile(r"([A-Z]+)([A-Z][a-z])")


class ConfiguredWorkspaces:
    """Manage agent-home workspace grants in one explicit config file."""

    def __init__(self, config_path: Path) -> None:
        self.config_path = config_path

    @property
    def lock_path(self) -> Path:
        return self.config_path.with_name(f".{self.config_path.name}.lock")

    def write_lock(self) -> AbstractContextManager[None]:
        """Return the lock shared by agent config mutations."""

        return file_write_lock(self.lock_path)

    def list(self) -> dict[str, str]:
        """Return configured workspace paths sorted by stable name."""

        content = _read_config_text(self.config_path)
        if content is None:
            return {}
        return self.parse(content)

    @staticmethod
    def parse(content: str) -> dict[str, str]:
        """Parse workspace grants without requiring their paths to exist."""

        config = cast(dict[str, object], tomllib.loads(content))
        raw_workspaces = config.get("workspaces")
        if raw_workspaces is None:
            return {}
        if not isinstance(raw_workspaces, Mapping):
            raise ValueError("workspaces config must be a table")
        workspaces: dict[str, str] = {}
        for raw_name, raw_path in sorted(raw_workspaces.items()):
            name = str(raw_name)
            _validate_workspace_name(name)
            if not isinstance(raw_path, str) or not raw_path:
                raise ValueError(f"workspace path must be a non-empty string: {name}")
            path = Path(raw_path)
            if not path.is_absolute():
                raise ValueError(f"workspace path must be absolute: {name}")
            workspaces[name] = str(path)
        _validate_workspace_roots(workspaces)
        return workspaces

    def add(self, path: Path, *, name: str | None = None) -> tuple[str, str]:
        """Grant one existing directory under a unique stable name."""

        candidate = path.expanduser()
        if not candidate.exists():
            raise FileNotFoundError(f"workspace directory not found: {candidate}")
        if not candidate.is_dir():
            raise ValueError(f"workspace path is not a directory: {candidate}")
        resolved = candidate.resolve(strict=True)
        default_name = (
            resolved.name if candidate.name in {"", ".", ".."} else candidate.name
        )
        selected_name = _normalize_workspace_name(
            name if name is not None else default_name
        )
        with self.write_lock():
            workspaces = self.list()
            if selected_name in workspaces:
                raise ValueError(f"workspace name already exists: {selected_name}")
            updated = {**workspaces, selected_name: str(resolved)}
            _validate_workspace_roots(updated)
            document = _load_config_document(self.config_path)
            table = _workspace_document_table(document, create=True)
            assert table is not None
            table[selected_name] = str(resolved)
            atomic_write_text(self.config_path, tomlkit.dumps(document))
        return selected_name, str(resolved)

    def remove(self, name: str) -> str:
        """Remove one workspace grant without changing the workspace itself."""

        with self.write_lock():
            workspaces = self.list()
            try:
                path = workspaces[name]
            except KeyError as exc:
                raise ValueError(f"workspace not found: {name}") from exc
            document = _load_config_document(self.config_path)
            table = _workspace_document_table(document, create=False)
            if table is None or name not in table:
                raise ValueError(f"workspace not found: {name}")
            del table[name]
            atomic_write_text(self.config_path, tomlkit.dumps(document))
        return path


def parse_config(content: bytes) -> dict[str, object]:
    """Parse one canonical State-owned UTF-8 TOML snapshot."""

    return cast(dict[str, object], tomllib.loads(content.decode("utf-8")))


def project_state_config(config: Mapping[str, object]) -> dict[str, object]:
    """Return the semantic config fields owned by durable Agent State."""

    projected: dict[str, object] = {}
    for name in _CAP_TABLES:
        value = config.get(name)
        if value is None:
            continue
        if not isinstance(value, Mapping):
            raise ValueError(f"invalid configured cap table: {name}")
        projected[name] = _mutable_mapping(cast(Mapping[str, object], value))
    raw_allow = config.get("allow")
    if raw_allow is not None:
        if not isinstance(raw_allow, Mapping):
            raise ValueError("allow config must be a table")
        allow_mapping = cast(Mapping[str, object], raw_allow)
        unknown = sorted(
            str(name)
            for name in allow_mapping
            if name not in {*CAP_ALLOW_FIELDS, "models", "tools"}
        )
        if unknown:
            raise ValueError(f"unknown allow field: {', '.join(unknown)}")
        allow = {
            name: _mutable_value(allow_mapping[name])
            for name in CAP_ALLOW_FIELDS
            if name in allow_mapping
        }
        if allow:
            projected["allow"] = allow
    return projected


def canonical_state_config(content: bytes) -> bytes:
    """Encode a deterministic TOML artifact containing only State-owned fields."""

    projected = project_state_config(
        cast(dict[str, object], tomllib.loads(content.decode("utf-8")))
    )
    return tomlkit.dumps(projected).encode("utf-8")


def resolve_cap_allows(
    configs: Sequence[Mapping[str, object]],
    *,
    overrides: Mapping[str, tuple[str, ...] | None] | None = None,
) -> dict[str, tuple[str, ...] | None]:
    """Resolve layered cap-kind allow fields and frozen startup replacements."""

    fields: dict[str, tuple[str, ...] | None] = {}
    for config in configs:
        raw_allow = config.get("allow")
        if raw_allow is None:
            continue
        if not isinstance(raw_allow, Mapping):
            raise ValueError("allow config must be a table")
        allow_mapping = cast(Mapping[str, object], raw_allow)
        for name in CAP_ALLOW_FIELDS:
            if name in allow_mapping:
                fields[name] = _query_values(name, allow_mapping[name])
    resolved_overrides = overrides or {}
    unknown = sorted(
        name for name in resolved_overrides if name not in CAP_ALLOW_FIELDS
    )
    if unknown:
        raise ValueError(f"unknown State allow override: {', '.join(unknown)}")
    for name, value in resolved_overrides.items():
        fields[name] = None if value is None else _query_values(name, value)
    return fields


def _query_values(name: str, value: object) -> tuple[str, ...] | None:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes | bytearray):
        raise TypeError(f"allow {name} must be an array of queries")
    try:
        return resolve_query_sentinels(
            cast(Sequence[str], value),
            label=f"allow {name}",
        )
    except ToolangError as error:
        raise ValueError(str(error)) from error


def _mutable_mapping(value: Mapping[str, object]) -> dict[str, object]:
    entries = {str(key): _mutable_value(item) for key, item in value.items()}
    return {key: entries[key] for key in sorted(entries)}


def _mutable_value(value: object) -> object:
    if isinstance(value, Mapping):
        return _mutable_mapping(cast(Mapping[str, object], value))
    if isinstance(value, tuple | list):
        return [_mutable_value(item) for item in value]
    return value


def _validate_workspace_name(name: str) -> None:
    if _WORKSPACE_NAME_RE.fullmatch(name) is None:
        raise ValueError(f"workspace name must use kebab case: {name!r}")


def _normalize_workspace_name(name: str) -> str:
    decomposed = unicodedata.normalize("NFKD", name)
    ascii_name = "".join(
        character for character in decomposed if not unicodedata.combining(character)
    )
    if not ascii_name.isascii():
        raise ValueError(f"workspace name contains unsupported characters: {name!r}")
    words = _ACRONYM_TO_WORD_RE.sub(r"\1-\2", ascii_name)
    words = _LOWER_TO_UPPER_RE.sub(r"\1-\2", words)
    normalized = _WORKSPACE_NAME_SEPARATOR_RE.sub("-", words.lower()).strip("-")
    if not normalized:
        raise ValueError(f"invalid workspace name: {name!r}")
    return normalized


def _validate_workspace_roots(workspaces: Mapping[str, str]) -> None:
    resolved = {
        name: _resolve_workspace_path(Path(path)) for name, path in workspaces.items()
    }
    names = tuple(resolved)
    for index, name in enumerate(names):
        path = resolved[name]
        for other_name in names[index + 1 :]:
            other_path = resolved[other_name]
            if _same_workspace_path(path, other_path):
                raise ValueError(f"workspace path already configured as {name}: {path}")
            if _workspace_path_is_within(path, other_path) or _workspace_path_is_within(
                other_path, path
            ):
                raise ValueError(
                    "workspace roots must not overlap: "
                    f"{name}={path}, {other_name}={other_path}"
                )


def _same_workspace_path(path: Path, other: Path) -> bool:
    if path == other:
        return True
    try:
        return path.samefile(other)
    except OSError:
        return False


def _workspace_path_is_within(path: Path, root: Path) -> bool:
    if path.is_relative_to(root):
        return True
    return any(_same_workspace_path(parent, root) for parent in path.parents)


def _resolve_workspace_path(path: Path) -> Path:
    try:
        return path.resolve(strict=False)
    except (OSError, RuntimeError):
        return Path(os.path.normpath(path))


def _read_config_text(path: Path) -> str | None:
    if not path.exists() and not path.is_symlink():
        return None
    if not path.is_file():
        raise ValueError(f"agent config must be a file: {path}")
    return path.read_text(encoding="utf-8")


def _load_config_document(path: Path) -> Any:
    return tomlkit.parse(_read_config_text(path) or "")


def _workspace_document_table(document: Any, *, create: bool) -> Any | None:
    value = document.get("workspaces")
    if value is not None:
        if not isinstance(value, Mapping):
            raise ValueError("workspaces config must be a table")
        return value
    if not create:
        return None
    value = tomlkit.table()
    document["workspaces"] = value
    return value
