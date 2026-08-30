"""CLI and environment parsing for frozen setup policy overrides."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation

from toolang.common.errors import ToolangError
from toolang.common.query import resolve_query_sentinels

_ALLOW_FIELDS = (
    "models",
    "tools",
    "psyches",
    "skills",
    "services",
    "prompts",
)
_BINDING_FIELDS = ("model", "runnable")
_LIMIT_FIELDS = (
    "agic_model_calls",
    "agic_tool_calls",
    "tokens",
    "cost",
    "time",
)


def resolve_ceiling_overrides(
    environ: Mapping[str, str],
    options: Sequence[str] | None = None,
) -> dict[str, tuple[str, ...] | None]:
    """Resolve frozen environment and CLI allow-list overrides."""

    resolved: dict[str, tuple[str, ...] | None] = {}
    for name in _ALLOW_FIELDS:
        raw = environ.get(f"TOOLANG_ALLOW_{name.upper()}")
        if raw is not None:
            resolved[name] = _parse_allow_value(name, raw, source="environment")
    resolved.update(_parse_allow_options(options or ()))
    return resolved


def resolve_binding_overrides(
    environ: Mapping[str, str],
    options: Sequence[str] | None = None,
) -> dict[str, str | None]:
    """Resolve frozen environment and CLI default-binding overrides."""

    resolved: dict[str, str | None] = {}
    for name in _BINDING_FIELDS:
        raw = environ.get(f"TOOLANG_DEFAULT_{name.upper()}")
        if raw is not None:
            resolved[name] = _parse_binding_value(name, raw, source="environment")
    resolved.update(_parse_binding_options(options or ()))
    return resolved


def resolve_limit_overrides(
    environ: Mapping[str, str],
    options: Sequence[str] | None = None,
) -> dict[str, int | Decimal | None]:
    """Resolve frozen environment and CLI run-limit overrides."""

    resolved: dict[str, int | Decimal | None] = {}
    for name in _LIMIT_FIELDS:
        raw = environ.get(f"TOOLANG_LIMIT_{name.upper()}")
        if raw is not None:
            resolved[name] = _parse_limit_value(name, raw, source="environment")
    resolved.update(_parse_limit_options(options or ()))
    return resolved


def _parse_allow_options(
    values: Sequence[str],
) -> dict[str, tuple[str, ...] | None]:
    parsed: dict[str, tuple[str, ...] | None] = {}
    for source in values:
        name, raw_value = _assignment(source, option="--allow")
        if name not in _ALLOW_FIELDS:
            raise ValueError(f"unknown allow field: {name}")
        value = _parse_allow_value(name, raw_value, source="--allow")
        if name not in parsed:
            parsed[name] = value
            continue
        current = parsed[name]
        if not current or not value:
            raise ValueError(f"--allow {name} cannot combine queries with all or none")
        if current is not None and value is not None:
            value = tuple(dict.fromkeys((*current, *value)))
        parsed[name] = value
    return parsed


def _parse_binding_options(values: Sequence[str]) -> dict[str, str | None]:
    parsed: dict[str, str | None] = {}
    for source in values:
        name, raw_value = _assignment(source, option="--default")
        if name not in _BINDING_FIELDS:
            raise ValueError(f"unknown default field: {name}")
        if name in parsed:
            raise ValueError(f"duplicate default field: {name}")
        parsed[name] = _parse_binding_value(name, raw_value, source="--default")
    return parsed


def _parse_limit_options(
    values: Sequence[str],
) -> dict[str, int | Decimal | None]:
    parsed: dict[str, int | Decimal | None] = {}
    for source in values:
        name, raw_value = _assignment(source, option="--limit")
        if name not in _LIMIT_FIELDS:
            raise ValueError(f"unknown run limit: {name}")
        if name in parsed:
            raise ValueError(f"duplicate run limit: {name}")
        parsed[name] = _parse_limit_value(name, raw_value, source="--limit")
    return parsed


def _assignment(value: str, *, option: str) -> tuple[str, str]:
    name, separator, raw_value = value.partition("=")
    name = name.strip()
    raw_value = raw_value.strip()
    if not separator or not name or not raw_value:
        raise ValueError(f"{option} expects one field=value assignment")
    return name, raw_value


def _parse_allow_value(
    name: str,
    value: str,
    *,
    source: str,
) -> tuple[str, ...] | None:
    text = value.strip()
    if not text:
        raise ValueError(f"{source} allow {name} must not be empty")
    try:
        return resolve_query_sentinels((text,), label=f"{source} allow {name}")
    except ToolangError as error:
        raise ValueError(str(error)) from error


def _parse_binding_value(name: str, value: str, *, source: str) -> str | None:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{source} default {name} must not be empty")
    return None if normalized.lower() == "none" else normalized


def _parse_limit_value(
    name: str,
    value: str,
    *,
    source: str,
) -> int | Decimal | None:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{source} limit {name} must not be empty")
    if normalized.lower() == "none":
        return None
    if name == "cost":
        try:
            parsed = Decimal(normalized)
        except InvalidOperation as exc:
            raise ValueError(f"{source} limit cost expects a decimal or none") from exc
        if not parsed.is_finite() or parsed < 0:
            raise ValueError(
                f"{source} limit cost expects a non-negative decimal or none"
            )
        return parsed
    try:
        parsed = int(normalized)
    except ValueError as exc:
        raise ValueError(f"{source} limit {name} expects an integer or none") from exc
    if parsed < 0:
        raise ValueError(
            f"{source} limit {name} expects a non-negative integer or none"
        )
    return parsed
