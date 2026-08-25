"""Settle-statement semantics."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING

from toolang.lang.ast import SettleStmt
from toolang.base.types.message import TextPart

from ...records import ControlRecord, StepPath
from ...types import IterationOccurrence, Occurrence, OccurrencePosition
from ..common import BoundRun
from ..common import Local, require_list
from ..steps import loop as loop_step

if TYPE_CHECKING:
    from ..executor import _Execution


async def execute(
    execution: _Execution,
    binding: BoundRun,
    locals: Mapping[str, Local],
    path: StepPath,
    statement: SettleStmt,
    controls: Sequence[ControlRecord],
    occurrence: Occurrence | None,
) -> Local:
    progress = loop_step.LoopProgress()

    async def evaluate() -> Local:
        source = locals.get("_", Local())
        item_type = source.type_name
        items = require_list(locals, operation="settle")
        progress.total = len(items)
        accumulator = Local((TextPart(""),), "item", type_name="Part[]")
        for index, item in enumerate(items):
            child_locals = dict(locals)
            child_locals["_"] = accumulator
            child_locals["item"] = Local(
                item,
                "item",
                ref=(
                    execution.item_pointer(source, index)
                    if source.ref is not None
                    else None
                ),
                type_name=item_type,
            )
            accumulator = await execution.execute_child(
                binding,
                child_locals,
                path,
                statement.runnable,
                Occurrence(
                    item=OccurrencePosition(index=index, count=len(items)),
                    iteration=IterationOccurrence(
                        index=index,
                        count=len(items),
                        phase="body",
                    ),
                ),
            )
            progress.iterations = index + 1
        return accumulator

    return await loop_step.execute(
        execution.emit,
        binding=binding,
        path=path,
        statement=statement,
        locals=locals,
        controls=controls,
        occurrence=occurrence,
        evaluate=evaluate,
        progress=progress,
    )
