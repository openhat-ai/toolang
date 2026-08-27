"""Shared model-call and tool-call value types."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, TypeAlias

from .message import Delta, Message, Part, PartType
from .tool import ToolDefinition

ModelContinuation: TypeAlias = dict[str, Any]


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
class ModelUsageMeter:
    """One provider-neutral or namespaced usage meter."""

    name: str
    quantity: Decimal
    unit: str

    def __post_init__(self) -> None:
        if not self.name or not self.unit:
            raise ValueError("model usage meter name and unit are required")
        if not self.quantity.is_finite() or self.quantity < 0:
            raise ValueError("model usage meter quantity must be non-negative")


@dataclass(frozen=True, slots=True)
class ModelUsage:
    """Inclusive token totals and optional precise billing components."""

    input_tokens: int
    output_tokens: int
    input_uncached_tokens: int | None = None
    input_cache_read_tokens: int | None = None
    input_cache_write_tokens: int | None = None
    input_audio_tokens: int | None = None
    output_visible_tokens: int | None = None
    output_reasoning_tokens: int | None = None
    output_audio_tokens: int | None = None
    meters: tuple[ModelUsageMeter, ...] = field(default_factory=tuple)
    reported_cost: Decimal | None = None
    reported_currency: str | None = None
    billing: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        values = (
            self.input_tokens,
            self.output_tokens,
            self.input_uncached_tokens,
            self.input_cache_read_tokens,
            self.input_cache_write_tokens,
            self.input_audio_tokens,
            self.output_visible_tokens,
            self.output_reasoning_tokens,
            self.output_audio_tokens,
        )
        if not isinstance(self.billing, dict):
            raise TypeError("model billing context must be an object")
        if any(
            value is not None
            and (isinstance(value, bool) or not isinstance(value, int) or value < 0)
            for value in values
        ):
            raise ValueError("model token usage must contain non-negative integers")
        for value in (
            self.input_uncached_tokens,
            self.input_cache_read_tokens,
            self.input_cache_write_tokens,
            self.input_audio_tokens,
        ):
            if value is not None and value > self.input_tokens:
                raise ValueError("model input usage component exceeds total input")
        for value in (
            self.output_visible_tokens,
            self.output_reasoning_tokens,
            self.output_audio_tokens,
        ):
            if value is not None and value > self.output_tokens:
                raise ValueError("model output usage component exceeds total output")
        input_components = (
            self.input_uncached_tokens,
            self.input_cache_read_tokens,
            self.input_cache_write_tokens,
        )
        if (
            sum(value for value in input_components if value is not None)
            > self.input_tokens
        ):
            raise ValueError("model input usage components exceed total input")
        if (
            self.output_visible_tokens is not None
            and self.output_reasoning_tokens is not None
            and self.output_visible_tokens + self.output_reasoning_tokens
            > self.output_tokens
        ):
            raise ValueError("model output usage components exceed total output")
        if self.reported_cost is not None and (
            not self.reported_cost.is_finite() or self.reported_cost < 0
        ):
            raise ValueError("reported model cost must be non-negative")
        if self.reported_cost is not None and not self.reported_currency:
            raise ValueError("reported model cost requires a currency")
        if self.reported_currency is not None and not self.reported_currency.strip():
            raise ValueError("reported model cost currency must be non-empty")
        if any(
            not isinstance(key, str)
            or not key.strip()
            or not isinstance(value, str)
            or not value.strip()
            for key, value in self.billing.items()
        ):
            raise ValueError("model billing context requires non-empty text values")


@dataclass(frozen=True, slots=True)
class ModelCall:
    """One normalized model call."""

    instructions: str
    messages: list[Message]
    tools: tuple[ToolDefinition, ...] = field(default_factory=tuple)
    cont: ModelContinuation | None = None


@dataclass(frozen=True, slots=True)
class ModelCallResult:
    """One completed model call."""

    message: Message | None = None
    tool_calls: tuple[ToolCall, ...] = field(default_factory=tuple)
    usage: ModelUsage | None = None
    cont: ModelContinuation | None = None


@dataclass(frozen=True, slots=True)
class ModelPartStart:
    """One streamed model-part start update."""

    kind: PartType


@dataclass(frozen=True, slots=True)
class ModelPartDelta:
    """One streamed model-part delta update."""

    delta: Delta


@dataclass(frozen=True, slots=True)
class ModelPartEnd:
    """One streamed model-part end update."""

    data: Part


ModelPartUpdate = ModelPartStart | ModelPartDelta | ModelPartEnd
ModelStreamHandler = Callable[[ModelPartUpdate], Awaitable[None]]
