"""Stable agent execution policy values."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class AgentCeiling:
    """Stable selector lists used to resolve one execution-tree ceiling."""

    models: tuple[str, ...] | None = None
    tools: tuple[str, ...] | None = None
    caps: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "models", _normalize_selectors(self.models, "model"))
        object.__setattr__(self, "tools", _normalize_selectors(self.tools, "tool"))
        object.__setattr__(self, "caps", _normalize_selectors(self.caps, "cap"))


@dataclass(frozen=True, slots=True)
class RunBindings:
    """Default model and runnable references for new root runs."""

    model: str | None = None
    runnable: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "model", _normalize_optional(self.model, "model"))
        runnable = _normalize_optional(self.runnable, "runnable")
        if runnable is not None:
            kind, separator, name = runnable.partition(":")
            if separator and (
                kind not in {"agic", "flow"}
                or not name
                or name != name.strip()
                or ":" in name
            ):
                raise ValueError(f"invalid run binding runnable: {runnable}")
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


def _normalize_selectors(
    values: tuple[str, ...] | None,
    name: str,
) -> tuple[str, ...] | None:
    if values is None:
        return None
    normalized: list[str] = []
    for value in values:
        if not isinstance(value, str):
            raise TypeError(f"{name} ceiling selectors must be strings")
        text = value.strip()
        if not text:
            raise ValueError(f"{name} ceiling selectors must not be empty")
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
