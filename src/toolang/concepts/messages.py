"""Canonical turn-message concepts.

This module defines the provider-independent message model used to describe
chat- and tool-oriented turn output. Stream protocols and persistence formats
should map into and out of these dataclasses instead of inventing parallel
message structures.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

MessageRole = Literal["system", "user", "assistant"]
TextPartState = Literal["streaming", "done"]
ToolPartState = Literal[
    "input-streaming",
    "input-available",
    "output-available",
    "output-error",
]


@dataclass(frozen=True, slots=True)
class TextPart:
    """One ordered text part inside a turn message."""

    id: str
    text: str
    state: TextPartState = "done"
    type: Literal["text"] = "text"


@dataclass(frozen=True, slots=True)
class ReasoningPart:
    """One ordered reasoning part inside a turn message."""

    id: str
    text: str
    state: TextPartState = "done"
    provider_metadata: dict[str, Any] = field(default_factory=dict)
    type: Literal["reasoning"] = "reasoning"


@dataclass(frozen=True, slots=True)
class ToolPart:
    """One ordered tool invocation part inside a turn message."""

    id: str
    tool_call_id: str
    state: ToolPartState
    tool_name: str | None = None
    tool_family: str | None = None
    input: Any = None
    output: Any = None
    error_text: str | None = None
    provider_metadata: dict[str, Any] = field(default_factory=dict)
    type: Literal["tool"] = "tool"


@dataclass(frozen=True, slots=True)
class SourceUrlPart:
    """One ordered source-url part inside a turn message."""

    id: str
    source_id: str
    url: str
    title: str | None = None
    provider_metadata: dict[str, Any] = field(default_factory=dict)
    type: Literal["source-url"] = "source-url"


@dataclass(frozen=True, slots=True)
class SourceDocumentPart:
    """One ordered source-document part inside a turn message."""

    id: str
    source_id: str
    media_type: str
    title: str | None = None
    filename: str | None = None
    provider_metadata: dict[str, Any] = field(default_factory=dict)
    type: Literal["source-document"] = "source-document"


@dataclass(frozen=True, slots=True)
class FilePart:
    """One ordered generic file/media part inside a turn message."""

    id: str
    file_id: str | None = None
    media_type: str | None = None
    name: str | None = None
    uri: str | None = None
    size_bytes: int | None = None
    provider_metadata: dict[str, Any] = field(default_factory=dict)
    type: Literal["file"] = "file"


MessagePart = (
    TextPart
    | ReasoningPart
    | ToolPart
    | SourceUrlPart
    | SourceDocumentPart
    | FilePart
)


@dataclass(frozen=True, slots=True)
class TurnMessage:
    """One canonical stored message inside a turn."""

    id: str
    role: MessageRole
    parts: tuple[MessagePart, ...] = ()
    created_at: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    provider_metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable representation."""

        return {
            "id": self.id,
            "role": self.role,
            "parts": [part_to_dict(part) for part in self.parts],
            "created_at": self.created_at,
            "metadata": dict(self.metadata),
            "provider_metadata": dict(self.provider_metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TurnMessage:
        """Load a canonical message from serialized data."""

        return cls(
            id=str(data["id"]),
            role=data["role"],
            parts=tuple(part_from_dict(item) for item in data.get("parts", [])),
            created_at=_string_or_none(data.get("created_at")),
            metadata=dict(data.get("metadata") or {}),
            provider_metadata=dict(data.get("provider_metadata") or {}),
        )

    def preview_text(self) -> str | None:
        """Return a compact preview derived from ordered text-like parts."""

        segments: list[str] = []
        for part in self.parts:
            if isinstance(part, TextPart | ReasoningPart):
                text = part.text.strip()
                if text:
                    segments.append(text)
        if not segments:
            return None
        return " ".join(segments)


def part_to_dict(part: MessagePart) -> dict[str, Any]:
    """Return a JSON-serializable representation for one part."""

    if isinstance(part, TextPart):
        return {
            "id": part.id,
            "type": part.type,
            "text": part.text,
            "state": part.state,
        }
    if isinstance(part, ReasoningPart):
        return {
            "id": part.id,
            "type": part.type,
            "text": part.text,
            "state": part.state,
            "provider_metadata": dict(part.provider_metadata),
        }
    if isinstance(part, ToolPart):
        return {
            "id": part.id,
            "type": part.type,
            "tool_call_id": part.tool_call_id,
            "tool_name": part.tool_name,
            "tool_family": part.tool_family,
            "state": part.state,
            "input": part.input,
            "output": part.output,
            "error_text": part.error_text,
            "provider_metadata": dict(part.provider_metadata),
        }
    if isinstance(part, SourceUrlPart):
        return {
            "id": part.id,
            "type": part.type,
            "source_id": part.source_id,
            "url": part.url,
            "title": part.title,
            "provider_metadata": dict(part.provider_metadata),
        }
    if isinstance(part, SourceDocumentPart):
        return {
            "id": part.id,
            "type": part.type,
            "source_id": part.source_id,
            "media_type": part.media_type,
            "title": part.title,
            "filename": part.filename,
            "provider_metadata": dict(part.provider_metadata),
        }
    return {
        "id": part.id,
        "type": part.type,
        "file_id": part.file_id,
        "media_type": part.media_type,
        "name": part.name,
        "uri": part.uri,
        "size_bytes": part.size_bytes,
        "provider_metadata": dict(part.provider_metadata),
    }


def part_from_dict(data: dict[str, Any]) -> MessagePart:
    """Load one canonical part from serialized data."""

    part_type = str(data["type"])
    if part_type == "text":
        return TextPart(
            id=str(data["id"]),
            text=str(data.get("text") or ""),
            state=data.get("state", "done"),
        )
    if part_type == "reasoning":
        return ReasoningPart(
            id=str(data["id"]),
            text=str(data.get("text") or ""),
            state=data.get("state", "done"),
            provider_metadata=dict(data.get("provider_metadata") or {}),
        )
    if part_type == "tool":
        return ToolPart(
            id=str(data["id"]),
            tool_call_id=str(data["tool_call_id"]),
            tool_name=_string_or_none(data.get("tool_name")),
            tool_family=_string_or_none(data.get("tool_family")),
            state=data["state"],
            input=data.get("input"),
            output=data.get("output"),
            error_text=_string_or_none(data.get("error_text")),
            provider_metadata=dict(data.get("provider_metadata") or {}),
        )
    if part_type == "source-url":
        return SourceUrlPart(
            id=str(data["id"]),
            source_id=str(data["source_id"]),
            url=str(data["url"]),
            title=_string_or_none(data.get("title")),
            provider_metadata=dict(data.get("provider_metadata") or {}),
        )
    if part_type == "source-document":
        return SourceDocumentPart(
            id=str(data["id"]),
            source_id=str(data["source_id"]),
            media_type=str(data["media_type"]),
            title=_string_or_none(data.get("title")),
            filename=_string_or_none(data.get("filename")),
            provider_metadata=dict(data.get("provider_metadata") or {}),
        )
    if part_type == "file":
        return FilePart(
            id=str(data["id"]),
            file_id=_string_or_none(data.get("file_id")),
            media_type=_string_or_none(data.get("media_type")),
            name=_string_or_none(data.get("name")),
            uri=_string_or_none(data.get("uri")),
            size_bytes=_int_or_none(data.get("size_bytes")),
            provider_metadata=dict(data.get("provider_metadata") or {}),
        )
    raise ValueError(f"unsupported message part type: {part_type}")


def _string_or_none(value: object) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text or None


def _int_or_none(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    return int(str(value))
