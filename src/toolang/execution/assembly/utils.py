"""Pure formatting helpers for model-call content."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from html import escape
from typing import cast

from toolang.base.types.message import Message, Part, TextPart

from ..types import MessageDelta, MessageTemplate, TypedRef, validate_runtime_value


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


def escape_markup_value(value: object) -> object:
    """Escape bundled template values without changing authored rendering."""

    if isinstance(value, str):
        return escape(value, quote=True)
    if isinstance(value, Mapping):
        return {key: escape_markup_value(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [escape_markup_value(item) for item in value]
    return value


def text_block(tag: str, content: str) -> str:
    """Keep rendered text inside a runtime-owned instruction or data block."""

    return f"<{tag}>\n{escape(content, quote=False)}\n</{tag}>" if content else ""


def strip_parts(parts: tuple[Part, ...]) -> tuple[Part, ...]:
    result = list(parts)
    if result and isinstance(result[0], TextPart):
        result[0] = TextPart(result[0].text.lstrip())
    if result and isinstance(result[-1], TextPart):
        result[-1] = TextPart(result[-1].text.rstrip())
    return tuple(part for part in result if not isinstance(part, TextPart) or part.text)


def join_parts(*groups: tuple[Part, ...]) -> tuple[Part, ...]:
    result: list[Part] = []
    for group in groups:
        if not group:
            continue
        if result:
            _append_part(result, TextPart("\n\n"))
        for part in group:
            _append_part(result, part)
    return tuple(result)


def _append_part(parts: list[Part], part: Part) -> None:
    if isinstance(part, TextPart) and parts and isinstance(parts[-1], TextPart):
        parts[-1] = TextPart(parts[-1].text + part.text)
    else:
        parts.append(part)
