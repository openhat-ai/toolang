"""Shared model-call and tool-call value types."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, cast

from .message import Delta, Message, MessagePart, MessagePartType
from .tool import ToolDefinition


@dataclass(frozen=True, slots=True)
class ToolCall:
    """One model-emitted tool call."""

    tool_call_id: str
    call_id: str
    name: str
    input: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ToolCallResult:
    """One completed local tool call."""

    tool_call_id: str
    call_id: str
    name: str
    input: dict[str, Any]
    output: dict[str, Any]
    error: str | None = None


@dataclass(frozen=True, slots=True)
class ModelUsage:
    """One model usage summary."""

    input_tokens: int
    output_tokens: int


@dataclass(frozen=True, slots=True)
class ModelCall:
    """One normalized model call."""

    instructions: str
    messages: list[Message]
    tools: tuple[ToolDefinition, ...] = field(default_factory=tuple)
    state: dict[str, Any] | None = None

    @classmethod
    def from_data(cls, payload: Mapping[str, Any]) -> ModelCall:
        instructions = payload.get("instructions")
        if not isinstance(instructions, str):
            raise ValueError("model call instructions must be text")
        raw_messages = payload.get("messages")
        if not isinstance(raw_messages, Sequence) or isinstance(
            raw_messages, (str, bytes, bytearray)
        ):
            raise ValueError("model call messages must be a list")
        raw_tools = payload.get("tools", ())
        if not isinstance(raw_tools, Sequence) or isinstance(
            raw_tools, (str, bytes, bytearray)
        ):
            raise ValueError("model call tools must be a list")
        state = payload.get("state")
        if state is not None and not isinstance(state, Mapping):
            raise ValueError("model call state must be an object")
        messages: list[Message] = []
        for index, item in enumerate(raw_messages):
            if not isinstance(item, Mapping):
                raise ValueError(f"model call message {index} must be an object")
            messages.append(Message.from_data(cast(Mapping[str, Any], item)))
        tools: list[ToolDefinition] = []
        for index, item in enumerate(raw_tools):
            if not isinstance(item, Mapping):
                raise ValueError(f"model call tool {index} must be an object")
            tools.append(
                ToolDefinition.from_data(cast(Mapping[str, Any], item))
            )
        return cls(
            instructions=instructions,
            messages=messages,
            tools=tuple(tools),
            state=dict(state) if isinstance(state, Mapping) else None,
        )

    def to_data(self) -> dict[str, Any]:
        return {
            "instructions": self.instructions,
            "messages": [message.to_data() for message in self.messages],
            "tools": [tool.to_data() for tool in self.tools],
            "state": dict(self.state) if self.state is not None else None,
        }


@dataclass(frozen=True, slots=True)
class ModelCallResult:
    """One completed model call."""

    message: Message | None = None
    tool_calls: tuple[ToolCall, ...] = field(default_factory=tuple)
    usage: ModelUsage | None = None
    state: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class ModelPartStart:
    """One streamed model-part start update."""

    kind: MessagePartType


@dataclass(frozen=True, slots=True)
class ModelPartDelta:
    """One streamed model-part delta update."""

    delta: Delta


@dataclass(frozen=True, slots=True)
class ModelPartEnd:
    """One streamed model-part end update."""

    data: MessagePart


ModelPartUpdate = ModelPartStart | ModelPartDelta | ModelPartEnd
ModelStreamHandler = Callable[[ModelPartUpdate], Awaitable[None]]
