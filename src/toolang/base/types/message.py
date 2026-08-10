"""Shared canonical message, part, and delta value types."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
import json
from typing import Any, Literal, cast


MessageRole = Literal["user", "assistant", "tool"]
MessagePartType = Literal[
    "text",
    "image",
    "audio",
    "document",
    "tool_call",
    "tool_result",
]
DeltaType = Literal["text", "tool_call"]
ImageDetail = Literal["low", "high", "auto", "original"]
AudioFormat = Literal["mp3", "wav"]


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
class ImagePart:
    """One canonical image input part."""

    image_url: str | None = None
    file_id: str | None = None
    detail: ImageDetail = "auto"
    filename: str | None = None
    media_type: str | None = None
    type: Literal["image"] = "image"

    def __post_init__(self) -> None:
        if sum(value is not None for value in (self.image_url, self.file_id)) != 1:
            raise ValueError("image part requires exactly one of image_url or file_id")

    @classmethod
    def from_data(cls, payload: Mapping[str, Any]) -> ImagePart:
        image_url = _optional_text(payload.get("image_url"))
        file_id = _optional_text(payload.get("file_id"))
        detail = _image_detail(payload.get("detail"))
        filename = _optional_text(payload.get("filename"))
        media_type = _optional_text(payload.get("media_type"))
        if image_url is None and file_id is None:
            raise ValueError("image part requires image_url or file_id")
        return cls(
            image_url=image_url,
            file_id=file_id,
            detail=detail,
            filename=filename,
            media_type=media_type,
        )

    def to_data(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "type": self.type,
            "detail": self.detail,
        }
        if self.image_url is not None:
            data["image_url"] = self.image_url
        if self.file_id is not None:
            data["file_id"] = self.file_id
        if self.filename is not None:
            data["filename"] = self.filename
        if self.media_type is not None:
            data["media_type"] = self.media_type
        return data


@dataclass(frozen=True, slots=True)
class AudioPart:
    """One canonical audio input part."""

    data: str
    format: AudioFormat
    filename: str | None = None
    media_type: str | None = None
    transcript: str | None = None
    type: Literal["audio"] = "audio"

    def __post_init__(self) -> None:
        if not self.data:
            raise ValueError("audio part requires data")
        if self.format not in {"mp3", "wav"}:
            raise ValueError(f"unsupported audio format: {self.format}")

    @classmethod
    def from_data(cls, payload: Mapping[str, Any]) -> AudioPart:
        data = _optional_text(payload.get("data"))
        media_type = _optional_text(payload.get("media_type"))
        if data is not None and data.startswith("data:"):
            parsed_media_type, parsed_data = _parse_data_url(data)
            media_type = media_type or parsed_media_type
            data = parsed_data
        if data is None:
            data_url = _optional_text(payload.get("data_url"))
            if data_url is None:
                raise ValueError("audio part requires data or data_url")
            parsed_media_type, parsed_data = _parse_data_url(data_url)
            media_type = media_type or parsed_media_type
            data = parsed_data
        format_value = _audio_format(payload.get("format"), media_type=media_type)
        filename = _optional_text(payload.get("filename"))
        transcript = _optional_text(payload.get("transcript"))
        return cls(
            data=data,
            format=format_value,
            filename=filename,
            media_type=media_type,
            transcript=transcript,
        )

    def to_data(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "type": self.type,
            "data": self.data,
            "format": self.format,
        }
        if self.filename is not None:
            data["filename"] = self.filename
        if self.media_type is not None:
            data["media_type"] = self.media_type
        if self.transcript is not None:
            data["transcript"] = self.transcript
        return data


@dataclass(frozen=True, slots=True)
class DocumentPart:
    """One canonical document part."""

    data: str | None = None
    url: str | None = None
    file_id: str | None = None
    filename: str | None = None
    media_type: str | None = None
    type: Literal["document"] = "document"

    def __post_init__(self) -> None:
        if sum(value is not None for value in (self.data, self.url, self.file_id)) != 1:
            raise ValueError(
                "document part requires exactly one of data, url, or file_id"
            )

    @classmethod
    def from_data(cls, payload: Mapping[str, Any]) -> DocumentPart:
        document_data = _optional_text(payload.get("data"))
        url = _optional_text(payload.get("url"))
        file_id = _optional_text(payload.get("file_id"))
        filename = _optional_text(payload.get("filename"))
        media_type = _optional_text(payload.get("media_type"))
        if document_data is not None and document_data.startswith("data:"):
            parsed_media_type, _parsed_data = _parse_data_url(document_data)
            media_type = media_type or parsed_media_type
        return cls(
            data=document_data,
            url=url,
            file_id=file_id,
            filename=filename,
            media_type=media_type,
        )

    def to_data(self) -> dict[str, Any]:
        result: dict[str, Any] = {"type": self.type}
        if self.data is not None:
            result["data"] = self.data
        if self.url is not None:
            result["url"] = self.url
        if self.file_id is not None:
            result["file_id"] = self.file_id
        if self.filename is not None:
            result["filename"] = self.filename
        if self.media_type is not None:
            result["media_type"] = self.media_type
        return result


@dataclass(frozen=True, slots=True)
class ToolCallPart:
    """One canonical tool-call part."""

    tool_call_id: str
    tool_name: str
    tool_family: str
    input: dict[str, Any] = field(default_factory=dict)
    call_id: str | None = None
    reasoning: str | None = None
    type: Literal["tool_call"] = "tool_call"

    @classmethod
    def from_data(cls, payload: Mapping[str, Any]) -> ToolCallPart:
        return cls(
            tool_call_id=str(payload.get("tool_call_id", "")),
            tool_name=str(payload.get("tool_name", "")),
            tool_family=str(
                payload.get("tool_family") or payload.get("tool_name") or ""
            ),
            input=_json_object(payload.get("input")),
            call_id=_optional_text(payload.get("call_id")),
            reasoning=_optional_text(payload.get("reasoning")),
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
        if self.reasoning:
            data["reasoning"] = self.reasoning
        return data


@dataclass(frozen=True, slots=True)
class ToolResultPart:
    """One canonical tool-result part."""

    tool_call_id: str
    tool_name: str
    tool_family: str
    output: dict[str, Any] = field(default_factory=dict)
    call_id: str | None = None
    error: str | None = None
    type: Literal["tool_result"] = "tool_result"

    @classmethod
    def from_data(cls, payload: Mapping[str, Any]) -> ToolResultPart:
        return cls(
            tool_call_id=str(payload.get("tool_call_id", "")),
            tool_name=str(payload.get("tool_name", "")),
            tool_family=str(
                payload.get("tool_family") or payload.get("tool_name") or ""
            ),
            output=_json_object(payload.get("output")),
            call_id=_optional_text(payload.get("call_id")),
            error=_optional_text(payload.get("error")),
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
        if self.error:
            data["error"] = self.error
        return data


PerceptPart = TextPart | ImagePart | AudioPart | DocumentPart
Percept = tuple[PerceptPart, ...]
MessagePart = PerceptPart | ToolCallPart | ToolResultPart


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


def part_from_data(payload: Mapping[str, Any]) -> MessagePart:
    """Return one canonical part from one serialized payload."""

    part_type = str(payload.get("type", "")).strip()
    if part_type == "text":
        return TextPart.from_data(payload)
    if part_type == "image":
        return ImagePart.from_data(payload)
    if part_type == "audio":
        return AudioPart.from_data(payload)
    if part_type == "document":
        return DocumentPart.from_data(payload)
    if part_type == "tool_call":
        return ToolCallPart.from_data(payload)
    if part_type == "tool_result":
        return ToolResultPart.from_data(payload)
    raise ValueError(f"unknown message part type: {part_type or '<empty>'}")


def parts_from_data(
    payloads: Sequence[Mapping[str, Any]],
) -> tuple[MessagePart, ...]:
    """Return canonical parts from one serialized sequence."""

    return tuple(part_from_data(item) for item in payloads)


def parts_to_data(parts: Sequence[MessagePart]) -> list[dict[str, Any]]:
    """Return serialized canonical parts."""

    return [part.to_data() for part in parts]


def message_text(parts: Sequence[MessagePart]) -> str:
    """Return concatenated text from canonical message parts."""

    return "".join(part.text for part in parts if isinstance(part, TextPart))


def message_summary(parts: Sequence[MessagePart]) -> str:
    """Return a compact human-readable summary for message parts."""

    items: list[str] = []
    for part in parts:
        if isinstance(part, TextPart):
            text = " ".join(part.text.split())
            if text:
                items.append(text)
            continue
        if isinstance(part, ImagePart):
            items.append(_part_label("image", filename=part.filename))
            continue
        if isinstance(part, AudioPart):
            items.append(_part_label("audio", filename=part.filename))
            continue
        if isinstance(part, DocumentPart):
            items.append(_part_label("document", filename=part.filename))
            continue
    return " ".join(items)


@dataclass(frozen=True, slots=True)
class Message:
    """Stable canonical message payload."""

    role: MessageRole
    parts: tuple[MessagePart, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if self.role == "user" and not all(
            isinstance(part, (TextPart, ImagePart, AudioPart, DocumentPart))
            for part in self.parts
        ):
            raise ValueError("user messages can only contain percept parts")
        if self.role == "assistant" and not all(
            isinstance(
                part,
                (TextPart, ImagePart, AudioPart, DocumentPart, ToolCallPart),
            )
            for part in self.parts
        ):
            raise ValueError(
                "assistant messages can only contain percept or tool-call parts"
            )
        if self.role == "tool" and (
            not self.parts
            or not all(isinstance(part, ToolResultPart) for part in self.parts)
        ):
            raise ValueError("tool messages require one or more tool-result parts")

    @classmethod
    def from_data(cls, payload: Mapping[str, Any]) -> Message:
        parts_payload = payload.get("parts")
        parts = (
            parts_from_data(
                [item for item in parts_payload if isinstance(item, Mapping)]
            )
            if isinstance(parts_payload, Sequence)
            and not isinstance(parts_payload, (str, bytes, bytearray))
            else ()
        )
        return cls(
            role=_message_role(payload.get("role")),
            parts=parts,
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
        error = payload.get("error")
        return cls(
            role="tool",
            parts=(
                ToolResultPart(
                    tool_call_id=call_id,
                    tool_name=tool_name,
                    tool_family=str(payload.get("family") or tool_name),
                    output=_json_object(payload.get("output")),
                    call_id=call_id,
                    error=str(error) if error is not None else None,
                ),
            ),
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

    @property
    def percept(self) -> Percept:
        """Return the message's content when it contains only percept parts."""

        if not all(
            isinstance(part, (TextPart, ImagePart, AudioPart, DocumentPart))
            for part in self.parts
        ):
            raise ValueError(f"{self.role} message is not a Percept")
        return cast(Percept, self.parts)

    def to_data(self) -> dict[str, Any]:
        return {"role": self.role, "parts": parts_to_data(self.parts)}


def _tool_output_payload(content: str) -> dict[str, Any]:
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        return {"output": {"content": content}}
    return (
        dict(parsed) if isinstance(parsed, Mapping) else {"output": {"content": parsed}}
    )


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


def _image_detail(value: object) -> ImageDetail:
    text = str(value or "auto").strip()
    if text not in {"low", "high", "auto", "original"}:
        raise ValueError(f"unsupported image detail: {text or '<empty>'}")
    return cast(ImageDetail, text)


def _audio_format(value: object, *, media_type: str | None = None) -> AudioFormat:
    text = str(value or "").strip().lower()
    if text in {"mp3", "wav"}:
        return cast(AudioFormat, text)
    inferred = _audio_format_from_media_type(media_type)
    if inferred is not None:
        return inferred
    raise ValueError(
        "audio part requires format=mp3|wav or one inferable from media_type/data_url"
    )


def _audio_format_from_media_type(media_type: str | None) -> AudioFormat | None:
    if media_type is None:
        return None
    text = media_type.strip().lower()
    if text in {"audio/mp3", "audio/mpeg"}:
        return "mp3"
    if text in {"audio/wav", "audio/x-wav", "audio/wave"}:
        return "wav"
    return None


def _parse_data_url(value: str) -> tuple[str | None, str]:
    prefix, separator, payload = value.partition(",")
    if not separator or not prefix.startswith("data:"):
        raise ValueError("expected a base64 data URL")
    header = prefix[5:]
    media_type, _, encoding = header.partition(";")
    if encoding.lower() != "base64":
        raise ValueError("data URL must use base64 encoding")
    data = payload.strip()
    if not data:
        raise ValueError("data URL is missing payload")
    return (media_type or None, data)


def _part_label(kind: str, *, filename: str | None) -> str:
    if filename:
        return f"[{kind}:{filename}]"
    return f"[{kind}]"
