"""Repeat-statement semantics."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from toolang.lang.ast import RepeatStmt

from ...records import ControlRecord, StepRef
from ...types import IterationOccurrence, Occurrence
from ..common import BoundRun
from ..common import Local, boolean
from ..iteration import (
    IterationScope,
    IterationFrame,
    snapshot,
    iteration_scope,
    history_available,
)
from ..steps import loop as loop_step

if TYPE_CHECKING:
    from ..executor import _Execution


async def execute(
    execution: _Execution,
    binding: BoundRun,
    locals: dict[str, Local],
    path: StepRef,
    statement: RepeatStmt,
    controls: Sequence[ControlRecord],
    occurrence: Occurrence | None,
) -> Local:
    progress = loop_step.LoopProgress(total=statement.count)

    async def evaluate() -> Local:
        child_index = 0
        iteration = 0
        scope = IterationScope(statement.window)
        if statement.runnable is not None:
            with iteration_scope(scope):
                history_available(
                    execution.condition_templates(binding, path, statement.runnable)
                )
        while statement.count is None or iteration < statement.count:
            entry = snapshot(locals)
            satisfied = False
            with iteration_scope(scope):
                child_index = await execution.execute_statements(
                    binding,
                    statement.stmts,
                    locals,
                    parent=path,
                    start=child_index,
                    occurrence=Occurrence(
                        iteration=IterationOccurrence(
                            index=iteration, count=statement.count, phase="body"
                        )
                    ),
                )
                if statement.runnable is not None:
                    templates = execution.condition_templates(
                        binding, path, statement.runnable
                    )
                    execution.validate_child_inputs(
                        binding, path, statement.runnable, locals
                    )
                    if history_available(templates):
                        condition = await execution.execute_child(
                            binding,
                            locals,
                            path,
                            statement.runnable,
                            Occurrence(
                                iteration=IterationOccurrence(
                                    index=iteration,
                                    count=statement.count,
                                    phase="until",
                                )
                            ),
                            output_binding=None,
                        )
                        satisfied = boolean(condition.value, operation="until")
            frame = IterationFrame(entry, snapshot(locals))
            scope = IterationScope(
                statement.window, (frame, *scope.frames)[: statement.window]
            )
            iteration += 1
            progress.iterations = iteration
            if satisfied:
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
