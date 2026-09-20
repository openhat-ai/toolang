"""Shared helpers for normalizing provider usage payloads."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import cast

from toolang.plugin import values


def field(value: object, name: str) -> object:
    """Read one field from either a decoded object or SDK response value."""

    return (
        cast(Mapping[str, object], value).get(name)
        if isinstance(value, Mapping)
        else getattr(value, name, None)
    )


def optional_int(value: object, name: str) -> int | None:
    """Read one non-negative integer field."""

    return values.optional_int(field(value, name), minimum=0)


def optional_text(value: object, name: str) -> str | None:
    """Read one non-empty text field."""

    return values.optional_text(field(value, name))


def optional_float(value: object, name: str) -> float | None:
    """Read one finite non-negative decimal field."""

    raw = field(value, name)
    if raw is None or isinstance(raw, bool):
        return None
    try:
        parsed = float(str(raw))
    except ValueError:
        return None
    return parsed if math.isfinite(parsed) and parsed >= 0 else None


def reported_cost(value: object) -> tuple[float | None, str | None]:
    """Normalize a provider-reported cost and its currency."""

    amount = optional_float(value, "cost")
    if amount is None:
        return None, None
    currency = (optional_text(value, "currency") or "USD").upper()
    return amount, currency


def billing_value(value: object, name: str) -> str | None:
    """Normalize one billing-context label."""

    text = optional_text(value, name)
    return text.lower() if text is not None else None
