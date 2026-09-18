"""Deterministic JSON encoding that preserves decimal values."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal
import json


def dumps(data: object, *, indent: int | None = 2) -> str:
    """Serialize JSON deterministically without converting Decimal through float."""

    separator = ": " if indent is not None else ":"
    item_separator = "," if indent is None else ","

    def encode(value: object, level: int) -> str:
        if value is None:
            return "null"
        if value is True:
            return "true"
        if value is False:
            return "false"
        if isinstance(value, str):
            return json.dumps(value, ensure_ascii=False)
        if isinstance(value, int) and not isinstance(value, bool):
            return str(value)
        if isinstance(value, Decimal):
            if not value.is_finite():
                raise ValueError("catalog decimals must be finite")
            return format(value, "f")
        if isinstance(value, float):
            return format(Decimal(str(value)), "f")
        if isinstance(value, Mapping):
            items = sorted(value.items(), key=lambda item: str(item[0]))
            if not items:
                return "{}"
            if indent is None:
                return (
                    "{"
                    + item_separator.join(
                        json.dumps(str(key), ensure_ascii=False)
                        + separator
                        + encode(item, level + 1)
                        for key, item in items
                    )
                    + "}"
                )
            prefix = " " * indent * (level + 1)
            closing = " " * indent * level
            return (
                "{\n"
                + ",\n".join(
                    prefix
                    + json.dumps(str(key), ensure_ascii=False)
                    + separator
                    + encode(item, level + 1)
                    for key, item in items
                )
                + f"\n{closing}}}"
            )
        if isinstance(value, Sequence) and not isinstance(
            value, (str, bytes, bytearray)
        ):
            if not value:
                return "[]"
            if indent is None:
                return (
                    "["
                    + item_separator.join(encode(item, level + 1) for item in value)
                    + "]"
                )
            prefix = " " * indent * (level + 1)
            closing = " " * indent * level
            return (
                "[\n"
                + ",\n".join(prefix + encode(item, level + 1) for item in value)
                + f"\n{closing}]"
            )
        raise TypeError(f"unsupported catalog JSON value: {type(value).__name__}")

    return encode(data, 0) + "\n"
