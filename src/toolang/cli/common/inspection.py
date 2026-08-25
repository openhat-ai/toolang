"""Canonical inspect target parsing shared by routing and commands."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TypeAlias

from toolang.execution.types import StepPath

_MODEL_CALL_TARGET = "model_call"
_MODEL_CALL_PREFIX = "model_call@"


@dataclass(frozen=True, slots=True)
class ThreadInspectTarget:
    """One thread inspection target."""

    thread_id: str


@dataclass(frozen=True, slots=True)
class RunInspectTarget:
    """One run inspection target."""

    run_id: str


@dataclass(frozen=True, slots=True)
class StepInspectTarget:
    """One durable StepPath inspection target."""

    step_path: StepPath


@dataclass(frozen=True, slots=True)
class HistoricalModelCallOwner:
    """One persisted model call owned by a model Step."""

    step_path: StepPath


@dataclass(frozen=True, slots=True)
class ProspectiveModelCallOwner:
    """The first model call prepared for the configured runnable."""


ModelCallOwner: TypeAlias = HistoricalModelCallOwner | ProspectiveModelCallOwner


@dataclass(frozen=True, slots=True)
class ModelCallInspectTarget:
    """One historical or prospective normalized model call."""

    owner: ModelCallOwner


InspectTarget: TypeAlias = (
    ThreadInspectTarget | RunInspectTarget | StepInspectTarget | ModelCallInspectTarget
)


def parse_inspect_target(target: str) -> InspectTarget:
    """Parse one canonical inspect target without reading external state."""

    value = target.strip()
    if not value:
        raise ValueError("inspect target is required")
    if value == _MODEL_CALL_TARGET:
        return ModelCallInspectTarget(ProspectiveModelCallOwner())
    if value.startswith(_MODEL_CALL_PREFIX):
        return ModelCallInspectTarget(_parse_model_call_owner(value))
    if ":" in value or "/" in value:
        raise ValueError(f"invalid inspect path: {value}")
    if "." in value:
        if not value.startswith("run_"):
            raise ValueError(f"invalid inspect path: {value}")
        try:
            return StepInspectTarget(StepPath.parse(value))
        except ValueError as exc:
            raise ValueError(f"invalid inspect path: {value}") from exc
    if value.startswith("run_"):
        return RunInspectTarget(value)
    return ThreadInspectTarget(value)


def inspect_target_requires_program(target: InspectTarget) -> bool:
    """Return whether routing must materialize authored program state."""

    return isinstance(target, ModelCallInspectTarget) and isinstance(
        target.owner,
        ProspectiveModelCallOwner,
    )


def _parse_model_call_owner(value: str) -> ModelCallOwner:
    owner = value.removeprefix(_MODEL_CALL_PREFIX)
    if not owner:
        raise ValueError("historical model_call target requires a StepPath")
    if not owner.startswith("run_") or "." not in owner:
        raise ValueError(f"model_call owner must be a StepPath: {owner}")
    try:
        return HistoricalModelCallOwner(StepPath.parse(owner))
    except ValueError as exc:
        raise ValueError(f"invalid model_call owner: {owner}") from exc
