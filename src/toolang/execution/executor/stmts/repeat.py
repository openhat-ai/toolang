"""Repeat-statement semantics."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from toolang.lang.ast import RepeatStmt

from ...records import RunControlRecord, StepPath
from ...types import IterationOccurrence, Occurrence
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
    occurrence: Occurrence | None,
) -> Local:
    progress = loop_step.LoopProgress(total=statement.count)

    async def evaluate() -> Local:
        child_index = 0
        iteration = 0
        while statement.count is None or iteration < statement.count:
            child_index = await execution.execute_statements(
                binding,
                statement.stmts,
                locals,
                parent=path,
                start=child_index,
                occurrence=Occurrence(
                    iteration=IterationOccurrence(
                        index=iteration,
                        count=statement.count,
                        phase="body",
                    )
                ),
            )
            iteration += 1
            progress.iterations = iteration
            if statement.runnable is not None:
                condition = await execution.execute_child(
                    binding,
                    locals,
                    path,
                    statement.runnable,
                    Occurrence(
                        iteration=IterationOccurrence(
                            index=iteration - 1,
                            count=statement.count,
                            phase="until",
                        )
                    ),
                    output_name=None,
                )
                if boolean(condition.value, operation="until"):
                    progress.termination = "satisfied"
                    break
        return Local()

    return await loop_step.execute(
        execution.emit,
        begin_step=execution.begin_step,
        binding=binding,
        path=path,
        statement=statement,
        locals=locals,
        controls=controls,
        occurrence=occurrence,
        evaluate=evaluate,
        progress=progress,
    )
