"""CLI parsing for run-limit overrides."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from decimal import Decimal, InvalidOperation

from toolang.base.types.run import RunLimits


_LIMIT_FIELDS = frozenset(
    {
        "agic_model_calls",
        "agic_tool_calls",
        "tokens",
        "cost",
        "time",
    }
)


def apply_limit_options(base: RunLimits, values: Sequence[str]) -> RunLimits:
    """Apply comma-separated CLI limit fields over one effective value."""

    parsed: dict[str, int | Decimal | None] = {}
    for source in values:
        for raw_item in source.split(","):
            item = raw_item.strip()
            if not item:
                raise ValueError("--limit contains an empty field")
            name, separator, raw_value = item.partition("=")
            name = name.strip()
            raw_value = raw_value.strip()
            if not separator or not name or not raw_value:
                raise ValueError("--limit expects field=value")
            if name not in _LIMIT_FIELDS:
                raise ValueError(f"unknown run limit: {name}")
            if name in parsed:
                raise ValueError(f"duplicate run limit: {name}")
            parsed[name] = _parse_limit_value(name, raw_value)
    return replace(base, **parsed)


def _parse_limit_value(name: str, value: str) -> int | Decimal | None:
    if value.lower() == "none":
        return None
    if name == "cost":
        try:
            parsed = Decimal(value)
        except InvalidOperation as exc:
            raise ValueError("--limit cost expects a decimal or none") from exc
        if not parsed.is_finite() or parsed < 0:
            raise ValueError("--limit cost expects a non-negative decimal or none")
        return parsed
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError(f"--limit {name} expects an integer or none") from exc
    if parsed < 0:
        raise ValueError(f"--limit {name} expects a non-negative integer or none")
    return parsed
