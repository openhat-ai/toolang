"""Repeat-statement semantics."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING

from toolang.lang.ast import RepeatStmt

from ...records import RunControlRecord, StepPath
from ..common import BoundRun
from ..common import Local, boolean
from ..steps import loop as loop_step

if TYPE_CHECKING:
    from ..executor import _Execution


async def execute(
    execution: _Execution,
    binding: BoundRun,
    locals: dict[str, Local],
    path: StepPath,
    statement: RepeatStmt,
    controls: Sequence[RunControlRecord],
    placement: Mapping[str, object] | None,
) -> Local:
    async def operation() -> Local:
        child_index = 0
        iteration = 0
        body_placement = (
            {"iters": statement.count} if statement.count is not None else {}
        )
        while statement.count is None or iteration < statement.count:
            child_index = await execution.execute_statements(
                binding,
                statement.stmts,
                locals,
                parent=path,
                start=child_index,
                placement={**body_placement, "iter": iteration},
            )
            iteration += 1
            if statement.until is not None:
                condition = await execution.execute_child(
                    binding,
                    locals,
                    path,
                    statement.until,
                    {**body_placement, "iter": -1},
                    output_name=None,
                )
                if boolean(condition.value, operation="until"):
                    break
        return Local()

    return await loop_step.execute(
        execution.emit,
        path=path,
        statement=statement,
        locals=locals,
        controls=controls,
        placement=placement,
        operation=operation,
    )
