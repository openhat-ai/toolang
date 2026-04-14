"""Shared canonical message, part, and delta value types."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
import json
from typing import Any, Literal, cast


MessageRole = Literal["user", "assistant", "tool"]
PartType = Literal["text", "tool_call", "tool_result"]
DeltaType = Literal["text", "tool_call"]


@dataclass(frozen=True, slots=True)
class TextPart:
    """One canonical text part."""

    text: str
    type: Literal["text"] = "text"

    @classmethod
    def from_data(cls, payload: Mapping[str, Any]) -> TextPart:
        return cls(text=str(payload.get("text", "")))

    def to_data(self) -> dict[str, Any]:
        return {"type": self.type, "text": self.text}


@dataclass(frozen=True, slots=True)
class ToolCallPart:
    """One canonical tool-call part."""

    tool_call_id: str
    tool_name: str
    tool_family: str
    input: dict[str, Any] = field(default_factory=dict)
    call_id: str | None = None
    type: Literal["tool_call"] = "tool_call"

    @classmethod
    def from_data(cls, payload: Mapping[str, Any]) -> ToolCallPart:
        return cls(
            tool_call_id=str(payload.get("tool_call_id", "")),
            tool_name=str(payload.get("tool_name", "")),
            tool_family=str(payload.get("tool_family") or payload.get("tool_name") or ""),
            input=_json_object(payload.get("input")),
            call_id=_optional_text(payload.get("call_id")),
        )

    def to_data(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "type": self.type,
            "tool_call_id": self.tool_call_id,
            "tool_name": self.tool_name,
            "tool_family": self.tool_family,
            "input": dict(self.input),
        }
        if self.call_id:
            data["call_id"] = self.call_id
        return data


@dataclass(frozen=True, slots=True)
class ToolResultPart:
    """One canonical tool-result part."""

    tool_call_id: str
    tool_name: str
    tool_family: str
    output: dict[str, Any] = field(default_factory=dict)
    call_id: str | None = None
    type: Literal["tool_result"] = "tool_result"

    @classmethod
    def from_data(cls, payload: Mapping[str, Any]) -> ToolResultPart:
        return cls(
            tool_call_id=str(payload.get("tool_call_id", "")),
            tool_name=str(payload.get("tool_name", "")),
            tool_family=str(payload.get("tool_family") or payload.get("tool_name") or ""),
            output=_json_object(payload.get("output")),
            call_id=_optional_text(payload.get("call_id")),
        )

    def to_data(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "type": self.type,
            "tool_call_id": self.tool_call_id,
            "tool_name": self.tool_name,
            "tool_family": self.tool_family,
            "output": dict(self.output),
        }
        if self.call_id:
            data["call_id"] = self.call_id
        return data


Part = TextPart | ToolCallPart | ToolResultPart


@dataclass(frozen=True, slots=True)
class TextDelta:
    """One canonical text delta."""

    text: str
    kind: Literal["text"] = "text"


@dataclass(frozen=True, slots=True)
class ToolCallDelta:
    """One canonical tool-call delta."""

    text: str
    tool_call_id: str
    kind: Literal["tool_call"] = "tool_call"


Delta = TextDelta | ToolCallDelta


def part_from_data(payload: Mapping[str, Any]) -> Part:
    """Return one canonical part from one serialized payload."""

    part_type = str(payload.get("type", "")).strip()
    if part_type == "text":
        return TextPart.from_data(payload)
    if part_type == "tool_call":
        return ToolCallPart.from_data(payload)
    if part_type == "tool_result":
        return ToolResultPart.from_data(payload)
    raise ValueError(f"unknown message part type: {part_type or '<empty>'}")


def parts_from_data(payloads: Sequence[Mapping[str, Any]]) -> tuple[Part, ...]:
    """Return canonical parts from one serialized sequence."""

    return tuple(part_from_data(item) for item in payloads)


def parts_to_data(parts: Sequence[Part]) -> list[dict[str, Any]]:
    """Return serialized canonical parts."""

    return [part.to_data() for part in parts]


def message_text(parts: Sequence[Part]) -> str:
    """Return concatenated text from canonical message parts."""

    return "".join(part.text for part in parts if isinstance(part, TextPart))


@dataclass(frozen=True, slots=True)
class Message:
    """Stable canonical message payload."""

    role: MessageRole
    parts: tuple[Part, ...] = field(default_factory=tuple)
    meta: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_data(cls, payload: Mapping[str, Any]) -> Message:
        parts_payload = payload.get("parts")
        parts = (
            parts_from_data([item for item in parts_payload if isinstance(item, Mapping)])
            if isinstance(parts_payload, Sequence) and not isinstance(parts_payload, (str, bytes, bytearray))
            else ()
        )
        meta = payload.get("meta")
        return cls(
            role=_message_role(payload.get("role")),
            parts=parts,
            meta=dict(meta) if isinstance(meta, Mapping) else {},
        )

    @classmethod
    def user(cls, text: str) -> Message:
        return cls(role="user", parts=(TextPart(text=text),))

    @classmethod
    def assistant(cls, text: str) -> Message:
        return cls(role="assistant", parts=(TextPart(text=text),))

    @classmethod
    def tool_output(cls, *, call_id: str, content: str) -> Message:
        payload = _tool_output_payload(content)
        tool_name = str(payload.get("name", ""))
        meta: dict[str, Any] = {}
        error = payload.get("error")
        if error is not None:
            meta["error"] = str(error)
        return cls(
            role="tool",
            parts=(
                ToolResultPart(
                    tool_call_id=call_id,
                    tool_name=tool_name,
                    tool_family=str(payload.get("family") or tool_name),
                    output=_json_object(payload.get("output")),
                    call_id=call_id,
                ),
            ),
            meta=meta,
        )

    @property
    def kind(self) -> str | None:
        if len(self.parts) != 1:
            return None
        return self.parts[0].type

    @property
    def content(self) -> str | None:
        if not all(isinstance(part, TextPart) for part in self.parts):
            return None
        return message_text(self.parts)

    def to_data(self) -> dict[str, Any]:
        data: dict[str, Any] = {"role": self.role, "parts": parts_to_data(self.parts)}
        if self.meta:
            data["meta"] = dict(self.meta)
        return data


def _tool_output_payload(content: str) -> dict[str, Any]:
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        return {"output": {"content": content}}
    return dict(parsed) if isinstance(parsed, Mapping) else {"output": {"content": parsed}}


def _json_object(raw: object) -> dict[str, Any]:
    if isinstance(raw, Mapping):
        return {str(key): value for key, value in raw.items()}
    if raw is None:
        return {}
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        if isinstance(parsed, Mapping):
            return {str(key): value for key, value in parsed.items()}
    return {}


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _message_role(value: object) -> MessageRole:
    text = str(value or "user").strip()
    if text not in {"user", "assistant", "tool"}:
        raise ValueError(f"unsupported message role: {text or '<empty>'}")
    return cast(MessageRole, text)
