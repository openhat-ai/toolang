"""Pure formatting helpers for model-call content."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from copy import deepcopy
from html import escape
from typing import cast

from toolang.base.types.message import Message, Part, TextPart

from ..types import MessageDelta, MessageTemplate, TypedRef, validate_runtime_value
from ..types import RecallTarget, RulesRecallTarget, WorkspaceRecallTarget


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
                for part in _render_segment(
                    segment, resolve, escape_text=message.escape_text
                )
            ),
        )
        for message in delta.messages
    )


def _render_segment(
    segment: str | Part | TypedRef,
    resolve: Callable[[TypedRef], object],
    *,
    escape_text: bool = False,
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
        text = cast(str, value)
        return (TextPart(escape(text, quote=False) if escape_text else text),)
    if segment.type.endswith("[]"):
        parts = deepcopy(tuple(cast(Sequence[Part], value)))
    else:
        parts = (deepcopy(cast(Part, value)),)
    return tuple(
        TextPart(escape(part.text, quote=False))
        if escape_text and isinstance(part, TextPart)
        else part
        for part in parts
    )


def resource_frame(
    target: RecallTarget, revision: str, content: str
) -> tuple[str, str]:
    """Frame a declaration without exposing internal bodyless revision markers."""
    tag = "toolang:" + {"skill": "skill-guidance", "service": "service-guidance"}.get(
        target.kind, target.kind
    )
    attrs = (
        {"workspace": target.workspace, "path": target.path}
        if isinstance(target, RulesRecallTarget)
        else {"ref": target.ref}
    )
    if revision == "0":
        attrs["removed"] = "true"
    elif content and not isinstance(target, WorkspaceRecallTarget):
        attrs["revision"] = revision
    attributes = " ".join(
        f'{key}="{escape(value, quote=True)}"' for key, value in attrs.items()
    )
    if revision == "0" or not content or isinstance(target, WorkspaceRecallTarget):
        return f"<{tag} {attributes}/>", ""
    return f"<{tag} {attributes}>", f"</{tag}>"


def resource_text(target: RecallTarget, revision: str, content: str) -> str:
    opening, closing = resource_frame(target, revision, content)
    return opening + (escape(content, quote=False) + closing if closing else "")


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
