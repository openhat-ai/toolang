"""Shared model-call and tool-call value types."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
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

    @classmethod
    def from_data(cls, payload: Mapping[str, Any]) -> RunLimits:
        """Build limits from their durable representation."""

        raw_cost = payload.get("cost")
        return cls(
            agic_model_calls=_optional_limit(
                "agic_model_calls", payload.get("agic_model_calls", 200)
            ),
            agic_tool_calls=_optional_limit(
                "agic_tool_calls", payload.get("agic_tool_calls")
            ),
            tokens=_optional_limit("tokens", payload.get("tokens")),
            cost=Decimal(str(raw_cost)) if raw_cost is not None else None,
            time=_optional_limit("time", payload.get("time")),
        )

    def to_data(self) -> dict[str, int | str | None]:
        """Return the stable durable representation of these limits."""

        return {
            "agic_model_calls": self.agic_model_calls,
            "agic_tool_calls": self.agic_tool_calls,
            "tokens": self.tokens,
            "cost": str(self.cost) if self.cost is not None else None,
            "time": self.time,
        }


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


def _validate_limit(name: str, value: int | None) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"run limit {name} must be an integer")
    if value < 0:
        raise ValueError(f"run limit {name} must be non-negative")


def _optional_limit(name: str, value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"run limit {name} must be an integer")
    return value
