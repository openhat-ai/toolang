"""Shared helpers for authored catalog files."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def normalize_meta(value: Mapping[Any, Any]) -> dict[str, object]:
    """Return normalized string-keyed authored-file metadata."""

    return {str(key): _normalize_value(item) for key, item in value.items()}


def _normalize_value(value: object) -> object:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, list | tuple):
        return [_normalize_value(item) for item in value]
    if isinstance(value, Mapping):
        return normalize_meta(value)
    isoformat = getattr(value, "isoformat", None)
    return str(isoformat()) if callable(isoformat) else str(value)
