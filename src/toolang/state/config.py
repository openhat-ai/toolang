"""State-owned configuration projection and policy parsing."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import tomllib
from typing import cast

import tomlkit

from toolang.catalog.types import CAP_DIR_BY_KIND, CAP_KINDS
from toolang.common.errors import ToolangError
from toolang.common.query import resolve_query_sentinels

CAP_ALLOW_FIELDS = tuple(f"{kind}s" for kind in CAP_KINDS)
_CAP_TABLES = tuple(CAP_DIR_BY_KIND[kind] for kind in CAP_KINDS)


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
