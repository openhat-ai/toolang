"""Loop statement steps."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass

from toolang.lang.ast import FlowStmt

from ...records import ControlRecord, StepRef
from ...types import LoopStepNoted, LoopTermination, Occurrence, StepStatus
from ..common import BoundRun, EventEmitter, Local, StepBoundary, execute_step


@dataclass(slots=True)
class LoopProgress:
    """Mutable loop outcome used to produce typed terminal noted facts."""

    iterations: int = 0
    termination: LoopTermination = "exhausted"
    total: int | None = None

    def noted(self, status: StepStatus) -> LoopStepNoted:
        termination: LoopTermination
        if status == "failed":
            termination = "failed"
        elif status == "canceled":
            termination = "canceled"
        else:
            termination = self.termination
        return LoopStepNoted(self.iterations, termination, self.total)


async def execute(
    emit: EventEmitter,
    *,
    begin_step: StepBoundary | None = None,
    binding: BoundRun,
    path: StepRef,
    statement: FlowStmt,
    locals: Mapping[str, Local],
    controls: Sequence[ControlRecord],
    occurrence: Occurrence | None,
    evaluate: Callable[[], Awaitable[Local]],
    progress: LoopProgress,
) -> Local:
    """Evaluate one loop Step and emit its event boundary."""

    return await execute_step(
        emit,
        begin_step=begin_step,
        kind="loop",
        path=path,
        binding=binding,
        statement=statement,
        locals=locals,
        controls=controls,
        occurrence=occurrence,
        evaluate=evaluate,
        note=progress.noted,
    )
