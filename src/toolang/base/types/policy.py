"""Stable agent execution policy values."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class AgentCeiling:
    """Stable queries that can only narrow agent resources."""

    models: tuple[str, ...] | None = None
    tools: tuple[str, ...] | None = None
    psyches: tuple[str, ...] | None = None
    skills: tuple[str, ...] | None = None
    services: tuple[str, ...] | None = None
    prompts: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "models", _normalize_queries(self.models, "model"))
        object.__setattr__(self, "tools", _normalize_queries(self.tools, "tool"))
        for name in ("psyches", "skills", "services", "prompts"):
            object.__setattr__(
                self,
                name,
                _normalize_queries(getattr(self, name), name.rstrip("s")),
            )


@dataclass(frozen=True, slots=True)
class RunBindings:
    """Model and runnable references bound to an execution policy."""

    model: str | None = None
    runnable: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "model", _normalize_optional(self.model, "model"))
        runnable = _normalize_optional(self.runnable, "runnable")
        object.__setattr__(
            self,
            "runnable",
            runnable,
        )


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
class RunPolicy:
    """Materialized caller policy for one root run."""

    allow: tuple[AgentCeiling, ...] = ()
    limits: RunLimits = field(default_factory=RunLimits)

    def __post_init__(self) -> None:
        if not isinstance(self.allow, tuple) or not all(
            isinstance(item, AgentCeiling) for item in self.allow
        ):
            raise TypeError("run policy allow must contain AgentCeiling values")
        if not isinstance(self.limits, RunLimits):
            raise TypeError("run policy limits must be RunLimits")


def _normalize_queries(
    values: tuple[str, ...] | None,
    name: str,
) -> tuple[str, ...] | None:
    if values is None:
        return None
    normalized: list[str] = []
    for value in values:
        if not isinstance(value, str):
            raise TypeError(f"{name} resource queries must be strings")
        text = value.strip()
        if not text:
            raise ValueError(f"{name} resource queries must not be empty")
        if text not in normalized:
            normalized.append(text)
    return tuple(normalized)


def _normalize_optional(value: str | None, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"run binding {name} must be a string")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"run binding {name} must not be empty")
    return normalized


def _validate_limit(name: str, value: int | None) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"run limit {name} must be an integer")
    if value < 0:
        raise ValueError(f"run limit {name} must be non-negative")
