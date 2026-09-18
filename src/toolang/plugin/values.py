"""Small shared readers for loosely typed plugin payloads."""

from __future__ import annotations

from collections.abc import Mapping, Sequence


def optional_text(value: object) -> str | None:
    """Return stripped non-empty text, or ``None``."""

    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def optional_int(value: object, *, minimum: int | None = None) -> int | None:
    """Return an integer honouring an inclusive minimum, or ``None``."""

    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if minimum is not None and value < minimum:
        return None
    return value


def mapping(value: object) -> dict[str, object]:
    """Return one decoded object with string keys, or an empty mapping."""

    if not isinstance(value, Mapping):
        return {}
    return {str(key): item for key, item in value.items()}


def compact_mapping(values: Mapping[str, object | None]) -> dict[str, object]:
    """Drop ``None`` values from one mapping."""

    return {key: value for key, value in values.items() if value is not None}


def string_tuple(value: object) -> tuple[str, ...]:
    """Return trimmed non-empty strings from one array value."""

    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return ()
    return tuple(
        item.strip() for item in value if isinstance(item, str) and item.strip()
    )
