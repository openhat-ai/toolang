"""Shared run and strategy value types."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .message import Delta, Message, Part, PartType
from .tool import ToolDefinition


@dataclass(frozen=True, slots=True)
class RunResult:
    """Final run-strategy result."""

    message: Message | None = None
    output_text: str = ""


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
class ModelPartStartEvent:
    """One streamed model-part start event."""

    kind: PartType


@dataclass(frozen=True, slots=True)
class ModelPartDeltaEvent:
    """One streamed model-part delta event."""

    delta: Delta


@dataclass(frozen=True, slots=True)
class ModelPartEndEvent:
    """One streamed model-part end event."""

    data: Part


ModelPartEvent = ModelPartStartEvent | ModelPartDeltaEvent | ModelPartEndEvent
ModelEventHandler = Callable[[ModelPartEvent], None]
