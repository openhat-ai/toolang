"""Settle-statement semantics."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING

from toolang.lang.ast import SettleStmt
from toolang.common.errors import ToolangError
from toolang.lang.input import coerce_output

from ...records import ControlRecord, StepRef
from ...types import IterationOccurrence, Occurrence, OccurrencePosition
from ..common import BoundRun
from ..common import Local, require_list, program_structs
from ..content import evaluate_content
from ..iteration import IterationScope, IterationFrame, snapshot, iteration_scope
from ..steps import loop as loop_step

if TYPE_CHECKING:
    from ..executor import _Execution


async def execute(
    execution: _Execution,
    binding: BoundRun,
    locals: Mapping[str, Local],
    path: StepRef,
    statement: SettleStmt,
    controls: Sequence[ControlRecord],
    occurrence: Occurrence | None,
) -> Local:
    progress = loop_step.LoopProgress()

    async def evaluate() -> Local:
        source = locals.get("_", Local())
        item_type = source.type_name
        items = require_list(locals, operation="settle", nonempty=True)

        def element(index: int) -> Local:
            return Local(
                items[index],
                "item",
                ref=source.ref.select(index) if source.ref is not None else None,
                type_name=item_type,
            )

        # Validate the complete batch before the first reducer can make a call.
        reducer = execution.validate_child_inputs(
            binding, path, statement.runnable, {**locals, "_": element(0)}
        )
        for index in range(1, len(items)):
            execution.validate_child_inputs(
                binding, path, statement.runnable, {**locals, "_": element(index)}
            )
        output_type = reducer.output or "Text"
        if statement.initial is None:
            if output_type != item_type:
                raise ToolangError(
                    f"settle without from requires {item_type} output, got {output_type}"
                )
            seed = element(0)
            start = 1
        else:
            seed = evaluate_content(execution, binding, locals, path, statement.initial)
            start = 0
        accumulator = Local(
            coerce_output(
                seed.value,
                output_type,
                structs=program_structs(
                    execution.current_binding(binding, *execution.state_for_step(path))
                ),
            ),
            "item",
            ref=seed.ref if seed.type_name == output_type else None,
            type_name=output_type,
        )
        scope = IterationScope(
            1, (IterationFrame(snapshot({}), snapshot({"_": accumulator})),)
        )
        progress.total = len(items) - start
        for index in range(start, len(items)):
            child_locals = {**locals, "_": element(index)}
            entry = snapshot(child_locals)
            with iteration_scope(scope):
                accumulator = await execution.execute_child(
                    binding,
                    child_locals,
                    path,
                    statement.runnable,
                    Occurrence(
                        item=OccurrencePosition(index=index, count=len(items)),
                        iteration=IterationOccurrence(
                            index=index - start, count=len(items) - start, phase="body"
                        ),
                    ),
                )
            scope = IterationScope(
                1,
                (IterationFrame(entry, snapshot({**child_locals, "_": accumulator})),),
            )
            progress.iterations = index - start + 1
        return accumulator

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
