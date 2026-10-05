"""Typed execution-value projections."""

from __future__ import annotations

from collections.abc import Mapping
import json
from typing import cast

from toolang.base.types.message import (
    AudioPart,
    DocumentPart,
    ImagePart,
    Part,
    content_parts,
    TextPart,
    ReasoningPart,
    ToolCallPart,
    ToolResultPart,
)
from toolang.lang.types import Array, Struct, Value, value_type

from .types import TypedRef

_PART_TYPES = (
    TextPart,
    ReasoningPart,
    ImagePart,
    AudioPart,
    DocumentPart,
    ToolCallPart,
    ToolResultPart,
)
_PART_ARRAY_TYPES = {
    "Part[]",
    "TextPart[]",
    "ReasoningPart[]",
    "ImagePart[]",
    "AudioPart[]",
    "DocumentPart[]",
    "ToolCallPart[]",
    "ToolResultPart[]",
}


def parts_from_value(
    value: Value | TypedRef, *, content_only: bool = False
) -> tuple[Part, ...]:
    """Project one resolved value into canonical message parts."""

    if _contains_pointer(value):
        raise ValueError("value must be resolved before projecting parts")
    if isinstance(value, _PART_TYPES):
        return content_parts((value,)) if content_only else (value,)
    if value_type(value) in _PART_ARRAY_TYPES:
        if not isinstance(value, Array) or not all(
            isinstance(item, _PART_TYPES) for item in value
        ):
            raise TypeError("Part[] value requires an ordered part sequence")
        return (
            content_parts(cast(tuple[Part, ...], tuple(value)))
            if content_only
            else cast(tuple[Part, ...], tuple(value))
        )
    if isinstance(value, str):
        return (TextPart(value),)
    return (
        TextPart(
            json.dumps(
                _presentation_data(value, content_only=content_only),
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            )
        ),
    )


def _contains_pointer(value: object) -> bool:
    if isinstance(value, TypedRef):
        return True
    if isinstance(value, Array | tuple | list):
        return any(_contains_pointer(item) for item in value)
    if isinstance(value, Struct | Mapping):
        return any(_contains_pointer(item) for item in value.values())
    return False


def _presentation_data(value: object, *, content_only: bool = False) -> object:
    if isinstance(value, _PART_TYPES):
        if content_only:
            visible = content_parts((value,))
            return {"$part": visible[0].to_data()} if visible else None
        return {"$part": value.to_data()}
    if isinstance(value, Array | tuple | list):
        return [
            _presentation_data(item, content_only=content_only)
            for item in value
            if not (content_only and isinstance(item, ReasoningPart))
        ]
    if isinstance(value, Struct | Mapping):
        return {
            str(name): _presentation_data(item, content_only=content_only)
            for name, item in value.items()
            if not (content_only and isinstance(item, ReasoningPart))
        }
    return value


__all__ = ["parts_from_value"]
