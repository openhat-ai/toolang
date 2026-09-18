"""Shared helpers for normalizing provider usage payloads."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import cast


def field(value: object, name: str) -> object:
    """Read one field from either a decoded object or SDK response value."""

    return (
        cast(Mapping[str, object], value).get(name)
        if isinstance(value, Mapping)
        else getattr(value, name, None)
    )


def optional_int(value: object, name: str) -> int | None:
    """Read one non-negative integer field."""

    raw = field(value, name)
    return (
        raw if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0 else None
    )


def optional_text(value: object, name: str) -> str | None:
    """Read one non-empty text field."""

    raw = field(value, name)
    if not isinstance(raw, str) or not raw.strip():
        return None
    return raw.strip()


def optional_decimal(value: object, name: str) -> Decimal | None:
    """Read one finite non-negative decimal field."""

    raw = field(value, name)
    if raw is None or isinstance(raw, bool):
        return None
    try:
        parsed = Decimal(str(raw))
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() and parsed >= 0 else None


def reported_cost(value: object) -> tuple[Decimal | None, str | None]:
    """Normalize a provider-reported cost and its currency."""

    amount = optional_decimal(value, "cost")
    if amount is None:
        return None, None
    currency = (optional_text(value, "currency") or "USD").upper()
    return amount, currency


def billing_value(value: object, name: str) -> str | None:
    """Normalize one billing-context label."""

    text = optional_text(value, name)
    return text.lower() if text is not None else None
