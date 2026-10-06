"""Repeat-statement semantics."""

from __future__ import annotations

import asyncio
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
    from toolang.state.state import AgentState
    from ...types import ControlRef
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

        def prepare_condition() -> tuple[
            AgentState, tuple[AgentState, ControlRef], tuple[str, ...]
        ]:
            assert statement.runnable is not None
            candidate = (
                execution.latest_state()
                if execution.executor._state is not None
                else binding.state
            )
            state, _ = execution.resolve_invocation(
                binding, statement.runnable, action="run", candidate_state=candidate
            )
            state_snapshot = (state, binding.state_ref)
            templates = execution.condition_templates(
                binding,
                path,
                statement.runnable,
                state_snapshot=state_snapshot,
                dependencies=candidate,
            )
            return candidate, state_snapshot, templates

        if statement.runnable is not None:
            with iteration_scope(scope):
                # Preflight the accepted code's history window even for N=0.
                # Live target/reentry checks belong at the authored condition.
                templates = execution.condition_templates(
                    binding,
                    path,
                    statement.runnable,
                    state_snapshot=(binding.state, binding.state_ref),
                    dependencies=(
                        execution.latest_state()
                        if execution.executor._state is not None
                        else binding.state
                    ),
                )
                history_available(templates)
        index = (
            len(statement.stmts)
            if statement.until_index is None
            else statement.until_index
        )
        while statement.count is None or iteration < statement.count:
            # Local-only bodies may never suspend at an execution boundary.
            await asyncio.sleep(0)
            execution.raise_if_canceling(binding.run_id, call=False)
            entry = snapshot(locals)
            satisfied = False
            body_occurrence = Occurrence(
                iteration=IterationOccurrence(
                    index=iteration, count=statement.count, phase="body"
                )
            )
            with iteration_scope(scope):
                child_index = await execution.execute_statements(
                    binding,
                    statement.stmts[:index],
                    locals,
                    parent=path,
                    start=child_index,
                    occurrence=body_occurrence,
                )
                if statement.runnable is not None:
                    candidate, state_snapshot, templates = prepare_condition()
                    execution.validate_child_inputs(
                        binding,
                        path,
                        statement.runnable,
                        locals,
                        state_snapshot=state_snapshot,
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
                            state_snapshot=state_snapshot,
                            candidate_state=candidate,
                        )
                        satisfied = boolean(condition.value, operation="until")
                if satisfied and index < len(statement.stmts):
                    progress.termination = "satisfied"
                    break
                child_index = await execution.execute_statements(
                    binding,
                    statement.stmts[index:],
                    locals,
                    parent=path,
                    start=child_index,
                    occurrence=body_occurrence,
                )
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

    execution.repeat_progress[path] = progress
    try:
        return await loop_step.execute(
            execution.emit,
            begin_step=execution.step_starter(binding),
            binding=binding,
            path=path,
            statement=statement,
            locals=locals,
            controls=controls,
            occurrence=occurrence,
            evaluate=evaluate,
            progress=progress,
        )
    finally:
        execution.repeat_progress.pop(path, None)
