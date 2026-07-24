"""Child-run statement steps."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import TYPE_CHECKING

from toolang.lang.ast import FlowStmt

from ...records import RunControlRecord, StepPath
from ..common import BoundRun
from ..common import Local, execute_step

if TYPE_CHECKING:
    from ..executor import _Execution


async def execute(
    execution: _Execution,
    *,
    binding: BoundRun,
    path: StepPath,
    statement: FlowStmt,
    locals: Mapping[str, Local],
    controls: Sequence[RunControlRecord],
    placement: Mapping[str, object] | None,
    runnable: str,
    transform: Callable[[Local], Local] | None = None,
    validate: Callable[[], None] | None = None,
) -> Local:
    """Execute one child-run operation and emit its run-step events."""

    async def operation() -> Local:
        if validate is not None:
            validate()
        result = await execution.execute_child(
            binding,
            locals,
            path,
            runnable,
            placement,
        )
        return transform(result) if transform is not None else result

    return await execute_step(
        execution.emit,
        kind="run",
        path=path,
        statement=statement,
        locals=locals,
        controls=controls,
        placement=placement,
        operation=operation,
    )
