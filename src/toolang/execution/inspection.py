"""Focused historical execution inspection vocabulary."""

from __future__ import annotations

from dataclasses import dataclass

from toolang.lang import format_statement_head

from .records import (
    ControlRecord,
    PreparationControlPayload,
    RunRecord,
    StepRecord,
    StoredModelStepGiven,
)
from .types import StepRef, ToolStepGiven


@dataclass(frozen=True, slots=True)
class InspectedRun:
    """One Run and the focused facts needed by ownership tables."""

    record: RunRecord
    entry: ControlRecord
    step_count: int

    @property
    def runnable(self) -> str:
        payload = self.entry.payload
        if self.entry.ref != self.record.control or not isinstance(
            payload, PreparationControlPayload
        ):
            raise ValueError(
                f"run preparation control not found: {self.record.control}"
            )
        return payload.runnable


@dataclass(frozen=True, slots=True)
class ChildOccurrenceTotals:
    """Consistent item and lane totals across direct visible child Runs."""

    items: int | None = None
    lanes: int | None = None


@dataclass(frozen=True, slots=True)
class InspectedStep:
    """One Step and its direct visible ownership facts."""

    record: StepRecord
    child_run_count: int
    child_step_count: int
    child_occurrence_totals: ChildOccurrenceTotals

    @property
    def operation(self) -> str:
        return step_operation(self.record)


@dataclass(frozen=True, slots=True)
class ExecutionSnapshot:
    """One transactionally consistent structural execution snapshot."""

    root: RunRecord | StepRecord
    runs: tuple[RunRecord, ...]
    steps: tuple[StepRecord, ...]
    entries: tuple[ControlRecord, ...]


def step_operation(step: StepRecord) -> str:
    """Return the durable operation owned by one Step."""

    if isinstance(step.given, StoredModelStepGiven):
        return step.given.model
    if isinstance(step.given, ToolStepGiven):
        return step.given.call.name
    return format_statement_head(step.given)


def direct_step_parent(ref: StepRef) -> StepRef | None:
    """Return the same-Run parent encoded by a StepRef."""

    return ref.parent


def run_fallback_order(run: RunRecord) -> tuple[str, str]:
    """Return the deterministic non-semantic Run order."""

    return (run.created_at, run.id)


def child_run_relation_order(
    parent: StepRecord,
    run: RunRecord,
) -> tuple[int, int, int, int, str, str]:
    """Order direct child Runs while keeping incomplete records diagnosable."""

    occur = run.occur
    if (
        parent.kind == "par"
        and occur is not None
        and occur.item is not None
        and occur.lane is not None
    ):
        return (0, occur.item.index, 0, 0, run.id, "")
    if parent.kind == "loop" and occur is not None and occur.iteration is not None:
        phase = 0 if occur.iteration.phase == "body" else 1
        item = occur.item.index if occur.item is not None else 0
        return (
            0,
            occur.iteration.index,
            phase,
            item,
            run.created_at,
            run.id,
        )
    created_at, run_id = run_fallback_order(run)
    return (1, 0, 0, 0, created_at, run_id)
