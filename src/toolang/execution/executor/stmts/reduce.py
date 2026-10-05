"""Reduce-statement semantics."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING

from toolang.lang.ast import ReduceStmt
from toolang.lang.contracts import OutputContract
from toolang.common.errors import ToolangError
from toolang.lang.input import coerce_output

from toolang.state.state import state_program
from ...runnables import resolve_call_target
from ...records import ControlRecord, StepRef
from ...types import IterationOccurrence, Occurrence, OccurrencePosition
from ...types import value_for_type
from ..common import BoundRun
from ..common import Local, _MISSING, require_list
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
    statement: ReduceStmt,
    controls: Sequence[ControlRecord],
    occurrence: Occurrence | None,
) -> Local:
    progress = loop_step.LoopProgress()

    async def evaluate() -> Local:
        source = locals.get("_", Local())
        item_type = source.element_type
        items = require_list(locals, operation="reduce", nonempty=True)

        def element(index: int) -> Local:
            return Local(
                items[index],
                ref=source.ref.select(index) if source.ref is not None else None,
                type_name=item_type,
            )

        # The implicit seed is output, not an input consumed by the reducer.
        reducer = execution.validate_child_inputs(
            binding,
            path,
            statement.runnable,
            {**locals, "_": element(0)},
            include_primary=statement.initial is not None,
        )
        for index in range(1, len(items)):
            execution.validate_child_inputs(
                binding, path, statement.runnable, {**locals, "_": element(index)}
            )
        output_type = reducer.output or "Text"
        target = resolve_call_target(binding.state, binding.module, statement.runnable)
        structs = {
            item.name: item
            for item in state_program(binding.state, target.module).structs
        }
        output_contract = OutputContract.resolve(output_type, structs=structs)
        if statement.initial is None:
            if output_type != item_type:
                raise ToolangError(
                    f"reduce without from requires {item_type} output, got {output_type}"
                )
            seed = element(0)
            start = 1
        else:
            seed = evaluate_content(execution, binding, locals, path, statement.initial)
            start = 0
        seed_ref = seed.ref if seed.type_name == output_type else None
        accumulator = Local(
            coerce_output(
                seed.value,
                output_type,
                structs=structs,
            ),
            ref=seed_ref,
            type_name=output_type,
            stored=value_for_type(output_type, seed_ref)
            if seed_ref is not None
            else _MISSING,
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
                    expected_output=output_contract,
                )
            scope = IterationScope(
                1,
                (IterationFrame(entry, snapshot({**child_locals, "_": accumulator})),),
            )
            progress.iterations = index - start + 1
        return accumulator

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
