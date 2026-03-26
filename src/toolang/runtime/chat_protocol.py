"""Chat protocol helpers and AI SDK-compatible stream mapping."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from toolang.concepts.messages import (
    MessageRole,
    TextPart,
    ToolPart,
    TurnMessage,
)
from toolang.concepts.tools import ToolCallResult
from toolang.runtime.model_exec import (
    TextDeltaEvent,
    ToolInputAvailableEvent,
    ToolInputDeltaEvent,
    ToolInputStartEvent,
    ToolOutputAvailableEvent,
)


@dataclass(frozen=True, slots=True)
class StartChunk:
    type: str = "start"


@dataclass(frozen=True, slots=True)
class TextStartChunk:
    id: str
    type: str = "text-start"


@dataclass(frozen=True, slots=True)
class TextDeltaChunk:
    id: str
    delta: str
    type: str = "text-delta"


@dataclass(frozen=True, slots=True)
class TextEndChunk:
    id: str
    type: str = "text-end"


@dataclass(frozen=True, slots=True)
class ToolInputStartChunk:
    id: str
    tool_call_id: str
    type: str = "tool-input-start"


@dataclass(frozen=True, slots=True)
class ToolInputDeltaChunk:
    id: str
    tool_call_id: str
    input_text_delta: str
    type: str = "tool-input-delta"


@dataclass(frozen=True, slots=True)
class ToolInputAvailableChunk:
    id: str
    tool_call_id: str
    tool_name: str
    input: Any
    provider_metadata: dict[str, Any] = field(default_factory=dict)
    type: str = "tool-input-available"


@dataclass(frozen=True, slots=True)
class ToolOutputAvailableChunk:
    id: str
    tool_call_id: str
    output: Any
    provider_metadata: dict[str, Any] = field(default_factory=dict)
    type: str = "tool-output-available"


@dataclass(frozen=True, slots=True)
class ToolOutputErrorChunk:
    id: str
    tool_call_id: str
    error_text: str
    provider_metadata: dict[str, Any] = field(default_factory=dict)
    type: str = "tool-output-error"


@dataclass(frozen=True, slots=True)
class FinishChunk:
    type: str = "finish"


@dataclass(frozen=True, slots=True)
class ErrorChunk:
    error_text: str
    type: str = "error"


AIMessageChunk = (
    StartChunk
    | TextStartChunk
    | TextDeltaChunk
    | TextEndChunk
    | ToolInputStartChunk
    | ToolInputDeltaChunk
    | ToolInputAvailableChunk
    | ToolOutputAvailableChunk
    | ToolOutputErrorChunk
    | FinishChunk
    | ErrorChunk
)


class TurnMessageBuilder:
    """Incrementally assemble one canonical assistant turn message."""

    def __init__(
        self,
        *,
        message_id: str,
        role: MessageRole = "assistant",
        created_at: str | None = None,
        metadata: dict[str, Any] | None = None,
        provider_metadata: dict[str, Any] | None = None,
    ) -> None:
        self._message_id = message_id
        self._role = role
        self._created_at = created_at
        self._metadata = dict(metadata or {})
        self._provider_metadata = dict(provider_metadata or {})
        self._parts: list[Any] = []
        self._next_part_index = 0
        self._tool_indexes: dict[str, int] = {}

    def append_text_delta(self, delta: str) -> None:
        """Append one streamed text delta to the trailing text part."""

        if not delta:
            return
        if self._parts and isinstance(self._parts[-1], TextPart):
            current = self._parts[-1]
            self._parts[-1] = replace(current, text=current.text + delta)
            return
        self._parts.append(
            TextPart(
                id=self._next_part_id("text"),
                text=delta,
                state="streaming",
            )
        )

    def tool_input_start(self, tool_call_id: str) -> None:
        """Append one pending tool part if it does not already exist."""

        if tool_call_id in self._tool_indexes:
            return
        self._tool_indexes[tool_call_id] = len(self._parts)
        self._parts.append(
            ToolPart(
                id=self._next_part_id("tool"),
                tool_call_id=tool_call_id,
                state="input-streaming",
            )
        )

    def tool_input_delta(self, tool_call_id: str, delta: str) -> None:
        """Append one streamed tool-input delta to an existing tool part."""

        index = self._require_tool_index(tool_call_id)
        current = self._parts[index]
        input_value = current.input if isinstance(current.input, str) else ""
        self._parts[index] = replace(
            current,
            state="input-streaming",
            input=input_value + delta,
        )

    def tool_input_available(
        self,
        *,
        tool_call_id: str,
        tool_name: str,
        input: Any,
        tool_family: str | None = None,
        provider_metadata: dict[str, Any] | None = None,
    ) -> None:
        """Set one parsed tool input on an existing tool part."""

        index = self._require_tool_index(tool_call_id)
        current = self._parts[index]
        self._parts[index] = replace(
            current,
            tool_name=tool_name,
            tool_family=tool_family,
            state="input-available",
            input=input,
            provider_metadata=dict(provider_metadata or current.provider_metadata),
        )

    def tool_output_available(
        self,
        *,
        tool_call_id: str,
        output: Any,
        tool_name: str | None = None,
        tool_family: str | None = None,
        provider_metadata: dict[str, Any] | None = None,
    ) -> None:
        """Set one successful tool output on an existing tool part."""

        index = self._require_tool_index(tool_call_id)
        current = self._parts[index]
        self._parts[index] = replace(
            current,
            tool_name=tool_name or current.tool_name,
            tool_family=tool_family or current.tool_family,
            state="output-available",
            output=output,
            provider_metadata=dict(provider_metadata or current.provider_metadata),
        )

    def tool_output_error(
        self,
        *,
        tool_call_id: str,
        error_text: str,
        tool_name: str | None = None,
        tool_family: str | None = None,
        provider_metadata: dict[str, Any] | None = None,
    ) -> None:
        """Set one failed tool output on an existing tool part."""

        index = self._require_tool_index(tool_call_id)
        current = self._parts[index]
        self._parts[index] = replace(
            current,
            tool_name=tool_name or current.tool_name,
            tool_family=tool_family or current.tool_family,
            state="output-error",
            error_text=error_text,
            provider_metadata=dict(provider_metadata or current.provider_metadata),
        )

    def apply_model_event(self, event: object) -> None:
        """Apply one runtime model event to this message builder."""

        if isinstance(event, TextDeltaEvent):
            self.append_text_delta(event.delta)
            return
        if isinstance(event, ToolInputStartEvent):
            self.tool_input_start(event.tool_call_id)
            return
        if isinstance(event, ToolInputDeltaEvent):
            self.tool_input_delta(event.tool_call_id, event.delta)
            return
        if isinstance(event, ToolInputAvailableEvent):
            self.tool_input_available(
                tool_call_id=event.tool_call_id,
                tool_name=event.name,
                tool_family=event.family,
                input=event.arguments,
                provider_metadata={
                    "toolang": {
                        "toolFamily": event.family,
                    }
                },
            )
            return
        if isinstance(event, ToolOutputAvailableEvent):
            if event.result.error is None:
                self.tool_output_available(
                    tool_call_id=event.tool_call_id,
                    tool_name=event.result.name,
                    tool_family=event.result.family,
                    output=event.result.output,
                    provider_metadata={
                        "toolang": {
                            "toolFamily": event.result.family,
                            "toolName": event.result.name,
                        }
                    },
                )
                return
            self.tool_output_error(
                tool_call_id=event.tool_call_id,
                tool_name=event.result.name,
                tool_family=event.result.family,
                error_text=event.result.error,
                provider_metadata={
                    "toolang": {
                        "toolFamily": event.result.family,
                        "toolName": event.result.name,
                    }
                },
            )

    def build(self) -> TurnMessage:
        """Return the final assembled turn message."""

        parts = tuple(
            replace(part, state="done")
            if isinstance(part, TextPart) and part.state == "streaming"
            else part
            for part in self._parts
        )
        return TurnMessage(
            id=self._message_id,
            role=self._role,
            parts=parts,
            created_at=self._created_at,
            metadata=dict(self._metadata),
            provider_metadata=dict(self._provider_metadata),
        )

    def _next_part_id(self, kind: str) -> str:
        self._next_part_index += 1
        return f"{self._message_id}:{kind}:{self._next_part_index}"

    def _require_tool_index(self, tool_call_id: str) -> int:
        if tool_call_id not in self._tool_indexes:
            self.tool_input_start(tool_call_id)
        return self._tool_indexes[tool_call_id]


class AIMessageChunkEncoder:
    """Encode runtime stream events into AI SDK-compatible message chunks."""

    def __init__(self, *, message_id: str) -> None:
        self._message_id = message_id
        self._text_started = False

    @property
    def has_text(self) -> bool:
        """Whether this stream has already emitted text chunks."""

        return self._text_started

    def start(self) -> list[AIMessageChunk]:
        return [StartChunk()]

    def encode_event(self, event: object) -> list[AIMessageChunk]:
        if isinstance(event, TextDeltaEvent):
            chunks: list[AIMessageChunk] = []
            if not self._text_started:
                self._text_started = True
                chunks.append(TextStartChunk(id=self._message_id))
            chunks.append(TextDeltaChunk(id=self._message_id, delta=event.delta))
            return chunks
        if isinstance(event, ToolInputStartEvent):
            return [
                ToolInputStartChunk(
                    id=self._message_id,
                    tool_call_id=event.tool_call_id,
                )
            ]
        if isinstance(event, ToolInputDeltaEvent):
            return [
                ToolInputDeltaChunk(
                    id=self._message_id,
                    tool_call_id=event.tool_call_id,
                    input_text_delta=event.delta,
                )
            ]
        if isinstance(event, ToolInputAvailableEvent):
            return [
                ToolInputAvailableChunk(
                    id=self._message_id,
                    tool_call_id=event.tool_call_id,
                    tool_name=event.name,
                    input=event.arguments,
                    provider_metadata={
                        "toolang": {
                            "toolFamily": event.family,
                        }
                    },
                )
            ]
        if isinstance(event, ToolOutputAvailableEvent):
            if event.result.error is None:
                return [
                    ToolOutputAvailableChunk(
                        id=self._message_id,
                        tool_call_id=event.tool_call_id,
                        output=event.result.output,
                        provider_metadata={
                            "toolang": {
                                "toolFamily": event.result.family,
                                "toolName": event.result.name,
                            }
                        },
                    )
                ]
            return [
                ToolOutputErrorChunk(
                    id=self._message_id,
                    tool_call_id=event.tool_call_id,
                    error_text=event.result.error,
                    provider_metadata={
                        "toolang": {
                            "toolFamily": event.result.family,
                            "toolName": event.result.name,
                        }
                    },
                )
            ]
        return []

    def finish(self) -> list[AIMessageChunk]:
        chunks: list[AIMessageChunk] = []
        if self._text_started:
            chunks.append(TextEndChunk(id=self._message_id))
        chunks.append(FinishChunk())
        return chunks

    def error(self, message: str) -> list[AIMessageChunk]:
        return [ErrorChunk(error_text=message), FinishChunk()]


def chunk_to_dict(chunk: AIMessageChunk) -> dict[str, Any]:
    """Return one JSON-serializable AI SDK-compatible chunk."""

    if isinstance(chunk, StartChunk | FinishChunk):
        return {"type": chunk.type}
    if isinstance(chunk, TextStartChunk):
        return {"type": chunk.type, "id": chunk.id}
    if isinstance(chunk, TextDeltaChunk):
        return {"type": chunk.type, "id": chunk.id, "delta": chunk.delta}
    if isinstance(chunk, TextEndChunk):
        return {"type": chunk.type, "id": chunk.id}
    if isinstance(chunk, ToolInputStartChunk):
        return {
            "type": chunk.type,
            "id": chunk.id,
            "toolCallId": chunk.tool_call_id,
        }
    if isinstance(chunk, ToolInputDeltaChunk):
        return {
            "type": chunk.type,
            "id": chunk.id,
            "toolCallId": chunk.tool_call_id,
            "inputTextDelta": chunk.input_text_delta,
        }
    if isinstance(chunk, ToolInputAvailableChunk):
        return {
            "type": chunk.type,
            "id": chunk.id,
            "toolCallId": chunk.tool_call_id,
            "toolName": chunk.tool_name,
            "input": chunk.input,
            "providerMetadata": dict(chunk.provider_metadata),
        }
    if isinstance(chunk, ToolOutputAvailableChunk):
        return {
            "type": chunk.type,
            "id": chunk.id,
            "toolCallId": chunk.tool_call_id,
            "output": chunk.output,
            "providerMetadata": dict(chunk.provider_metadata),
        }
    if isinstance(chunk, ToolOutputErrorChunk):
        return {
            "type": chunk.type,
            "id": chunk.id,
            "toolCallId": chunk.tool_call_id,
            "errorText": chunk.error_text,
            "providerMetadata": dict(chunk.provider_metadata),
        }
    return {
        "type": chunk.type,
        "errorText": chunk.error_text,
    }


def build_assistant_turn_message(
    *,
    message_id: str,
    output_text: str,
    tool_calls: list[ToolCallResult],
    created_at: str | None = None,
    metadata: dict[str, Any] | None = None,
    provider_metadata: dict[str, Any] | None = None,
) -> TurnMessage:
    """Build one final assistant turn message from completed turn output."""

    builder = TurnMessageBuilder(
        message_id=message_id,
        created_at=created_at,
        metadata=metadata,
        provider_metadata=provider_metadata,
    )
    for index, item in enumerate(tool_calls, start=1):
        tool_call_id = f"{message_id}:tool:{index}"
        builder.tool_input_start(tool_call_id)
        builder.tool_input_available(
            tool_call_id=tool_call_id,
            tool_name=item.name,
            tool_family=item.family,
            input=item.arguments,
            provider_metadata={
                "toolang": {
                    "toolFamily": item.family,
                }
            },
        )
        if item.error is None:
            builder.tool_output_available(
                tool_call_id=tool_call_id,
                tool_name=item.name,
                tool_family=item.family,
                output=item.output,
                provider_metadata={
                    "toolang": {
                        "toolFamily": item.family,
                        "toolName": item.name,
                    }
                },
            )
        else:
            builder.tool_output_error(
                tool_call_id=tool_call_id,
                tool_name=item.name,
                tool_family=item.family,
                error_text=item.error,
                provider_metadata={
                    "toolang": {
                        "toolFamily": item.family,
                        "toolName": item.name,
                    }
                },
            )
    if output_text:
        builder.append_text_delta(output_text)
    return builder.build()
