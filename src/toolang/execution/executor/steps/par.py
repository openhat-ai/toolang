"""Parallel statement steps."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence

from toolang.lang.ast import FlowStmt

from ...records import ControlRecord, StepRef
from ...types import Occurrence
from ..common import BoundRun, EventEmitter, Local, StepBoundary, execute_step


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
) -> Local:
    """Evaluate one parallel Step and emit its event boundary."""

    return await execute_step(
        emit,
        begin_step=begin_step,
        kind="par",
        path=path,
        binding=binding,
        statement=statement,
        locals=locals,
        controls=controls,
        occurrence=occurrence,
        evaluate=evaluate,
    )
