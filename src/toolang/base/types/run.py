"""Shared model-call and tool-call value types."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from decimal import Decimal
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
class RunLimits:
    """Limits applied to one root run tree."""

    agic_model_calls: int | None = 200
    agic_tool_calls: int | None = None
    tokens: int | None = None
    cost: Decimal | None = None
    time: int | None = None

    def __post_init__(self) -> None:
        _validate_limit("agic_model_calls", self.agic_model_calls)
        _validate_limit("agic_tool_calls", self.agic_tool_calls)
        _validate_limit("tokens", self.tokens)
        _validate_limit("time", self.time)
        if self.cost is not None:
            if not isinstance(self.cost, Decimal):
                raise TypeError("run limit cost must be a Decimal")
            if not self.cost.is_finite() or self.cost < 0:
                raise ValueError("run limit cost must be finite and non-negative")

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


def _validate_limit(name: str, value: int | None) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"run limit {name} must be an integer")
    if value < 0:
        raise ValueError(f"run limit {name} must be non-negative")
