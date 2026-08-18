"""Typed execution-value projections."""

from __future__ import annotations

from collections.abc import Mapping
import json
from typing import cast

from toolang.base.types.message import (
    AudioPart,
    DocumentPart,
    ImagePart,
    MessagePart,
    TextPart,
    ToolCallPart,
    ToolResultPart,
)
from toolang.lang.types import Array, Struct

from .types import Local, TypedPointer

_PART_TYPES = (
    TextPart,
    ImagePart,
    AudioPart,
    DocumentPart,
    ToolCallPart,
    ToolResultPart,
)
_PART_ARRAY_TYPES = {
    "Part[]",
    "TextPart[]",
    "ImagePart[]",
    "AudioPart[]",
    "DocumentPart[]",
    "ToolCallPart[]",
    "ToolResultPart[]",
}


def parts_from_local(local: Local) -> tuple[MessagePart, ...]:
    """Project one resolved typed local into canonical message parts."""

    value = local.value
    if _contains_pointer(value):
        raise ValueError("local must be resolved before projecting parts")
    if isinstance(value, _PART_TYPES):
        return (value,)
    if local.type in _PART_ARRAY_TYPES:
        if not isinstance(value, Array) or not all(
            isinstance(item, _PART_TYPES) for item in value
        ):
            raise TypeError("Part[] local requires an ordered part sequence")
        return cast(tuple[MessagePart, ...], tuple(value))
    if local.type == "Text":
        if not isinstance(value, str):
            raise TypeError("Text local requires text")
        return (TextPart(value),)
    return (
        TextPart(
            json.dumps(
                _presentation_data(value),
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            )
        ),
    )


def _contains_pointer(value: object) -> bool:
    if isinstance(value, TypedPointer):
        return True
    if isinstance(value, Array | tuple):
        return any(_contains_pointer(item) for item in value)
    if isinstance(value, Struct | Mapping):
        return any(_contains_pointer(item) for item in value.values())
    return False


def _presentation_data(value: object) -> object:
    if isinstance(value, _PART_TYPES):
        return {"$part": value.to_data()}
    if isinstance(value, Array | tuple):
        return [_presentation_data(item) for item in value]
    if isinstance(value, Struct | Mapping):
        return {str(name): _presentation_data(item) for name, item in value.items()}
    return value


__all__ = ["parts_from_local"]
