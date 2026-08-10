"""Shared model-call and tool-call value types."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

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
