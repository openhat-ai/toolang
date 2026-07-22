"""Agent-interaction statement steps."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence

from toolang.lang.ast import FlowStmt

from ...records import RunControlRecord, StepPath
from ..common import Local, EventEmitter, execute_step


async def execute(
    emit: EventEmitter,
    *,
    path: StepPath,
    statement: FlowStmt,
    locals: Mapping[str, Local],
    controls: Sequence[RunControlRecord],
    placement: Mapping[str, object] | None,
    operation: Callable[[], Awaitable[Local]],
) -> Local:
    """Execute one agent interaction and emit its step events."""

    return await execute_step(
        emit,
        kind="agent",
        path=path,
        statement=statement,
        locals=locals,
        controls=controls,
        placement=placement,
        operation=operation,
    )
