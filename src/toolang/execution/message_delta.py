"""Versioned, deterministic expansion of durable message deltas."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from typing import Any, cast

from toolang.base.types.message import (
    Message,
    MessageRole,
    Part,
    TextPart,
    part_from_data,
)

from .types import MessageDelta, MessageTemplate, TypedRef, validate_runtime_value

_PART_NAMES = {
    "Part",
    "TextPart",
    "ImagePart",
    "AudioPart",
    "DocumentPart",
    "ToolCallPart",
    "ToolResultPart",
}


def literal_delta(messages: Sequence[Message]) -> MessageDelta:
    """Record already-rendered content without guessing its provenance."""

    return MessageDelta(
        messages=tuple(
            MessageTemplate(
                message.role,
                tuple(
                    part.text if isinstance(part, TextPart) else deepcopy(part)
                    for part in message.parts
                ),
            )
            for message in messages
        )
    )


def render_delta(
    delta: MessageDelta, resolve: Callable[[TypedRef], object]
) -> tuple[Message, ...]:
    """Expand a delta using live or durable values under the same format rules."""

    if delta.version != 1:
        raise ValueError(f"unsupported message delta version: {delta.version}")
    return tuple(
        Message(
            role=message.role,
            parts=tuple(
                part
                for segment in message.segments
                for part in _render_segment(segment, resolve)
            ),
        )
        for message in delta.messages
    )


def _render_segment(
    segment: str | Part | TypedRef, resolve: Callable[[TypedRef], object]
) -> tuple[Part, ...]:
    if isinstance(segment, str):
        return (TextPart(segment),)
    if isinstance(segment, Part):
        return (deepcopy(segment),)
    if segment.type != "Text" and segment.type.removesuffix("[]") not in _PART_NAMES:
        raise ValueError(f"message segment requires Text or Parts: {segment}")
    value = resolve(segment)
    validate_runtime_value(value, segment.type)
    if segment.type == "Text":
        return (TextPart(cast(str, value)),)
    if segment.type.endswith("[]"):
        return deepcopy(tuple(cast(Sequence[Part], value)))
    return (deepcopy(cast(Part, value)),)


def delta_to_data(delta: MessageDelta) -> dict[str, object]:
    """Serialize only top-level segments as references or canonical Parts."""

    return {
        "version": delta.version,
        "messages": [
            {
                "role": message.role,
                "segments": [
                    segment
                    if isinstance(segment, str)
                    else {"?": str(segment)}
                    if isinstance(segment, TypedRef)
                    else segment.to_data()
                    for segment in message.segments
                ],
            }
            for message in delta.messages
        ],
    }


def delta_from_data(data: Mapping[str, object]) -> MessageDelta:
    """Decode a stored delta without interpreting literal text or nested data."""

    if data["version"] != 1:
        raise ValueError(f"unsupported message delta version: {data['version']}")
    messages = cast(Sequence[Mapping[str, object]], data["messages"])
    return MessageDelta(
        version=cast(int, data["version"]),
        messages=tuple(
            MessageTemplate(
                role=cast(MessageRole, message["role"]),
                segments=tuple(
                    _segment_from_data(segment)
                    for segment in cast(
                        Sequence[str | Mapping[str, Any]], message["segments"]
                    )
                ),
            )
            for message in messages
        ),
    )


def _segment_from_data(data: str | Mapping[str, Any]) -> str | Part | TypedRef:
    if isinstance(data, str):
        return data
    if set(data) == {"?"}:
        return TypedRef.parse(data["?"])
    return part_from_data(data)
