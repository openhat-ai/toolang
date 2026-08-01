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
    locals: Mapping[str, Local],
    path: StepPath,
    statement: RepeatStmt,
    controls: Sequence[RunControlRecord],
    placement: Mapping[str, object] | None,
) -> Local:
    async def operation() -> Local:
        working = dict(locals)
        child_index = 0
        iteration = 0
        while statement.count is None or iteration < statement.count:
            child_index = await execution.execute_statements(
                binding,
                statement.stmts,
                working,
                parent=path,
                start=child_index,
                placement={"loop": iteration},
            )
            iteration += 1
            if statement.until is not None:
                condition = await execution.execute_child(
                    binding,
                    working,
                    path,
                    statement.until,
                    {"loop": iteration - 1, "role": "until"},
                )
                if boolean(condition.value, operation="until"):
                    break
        return working.get("_", Local())

    return await loop_step.execute(
        execution.emit,
        path=path,
        statement=statement,
        locals=locals,
        controls=controls,
        placement=placement,
        operation=operation,
    )
