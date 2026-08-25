"""Parallel statement steps."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence

from toolang.lang.ast import FlowStmt

from ...records import ControlRecord, StepPath
from ...types import Occurrence
from ..common import BoundRun, EventEmitter, ItemPointer, Local, execute_step


async def execute(
    emit: EventEmitter,
    *,
    binding: BoundRun,
    path: StepPath,
    statement: FlowStmt,
    locals: Mapping[str, Local],
    controls: Sequence[ControlRecord],
    occurrence: Occurrence | None,
    evaluate: Callable[[], Awaitable[Local]],
    item_pointer: ItemPointer | None = None,
) -> Local:
    """Evaluate one parallel Step and emit its event boundary."""

    return await execute_step(
        emit,
        kind="par",
        path=path,
        binding=binding,
        statement=statement,
        locals=locals,
        controls=controls,
        occurrence=occurrence,
        evaluate=evaluate,
        item_pointer=item_pointer,
    )
