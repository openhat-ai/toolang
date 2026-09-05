"""Keep- and drop-statement semantics."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING

from toolang.common.errors import ToolangError
from toolang.lang.ast import DropStmt, KeepStmt

from ...records import ControlRecord, StepRef
from ...types import Occurrence
from ..common import BoundRun
from ..common import Local, require_list
from ..steps import par as par_step
from ..steps import value as value_step

if TYPE_CHECKING:
    from ..executor import _Execution


async def execute(
    execution: _Execution,
    binding: BoundRun,
    locals: Mapping[str, Local],
    path: StepRef,
    statement: KeepStmt | DropStmt,
    controls: Sequence[ControlRecord],
    occurrence: Occurrence | None,
) -> Local:
    async def evaluate() -> Local:
        items = require_list(locals, operation=statement.kind)
        if statement.position is not None:
            count = statement.count or 0
            selected = set(
                range(min(count, len(items)))
                if statement.position == "first"
                else range(max(len(items) - count, 0), len(items))
            )
            matches = [index in selected for index in range(len(items))]
            return Local(matches, "list", type_name="Boolean")
        else:
            if statement.runnable is None:
                raise ToolangError(f"{statement.kind} requires a predicate")
            return await execution.parallel_children(
                binding,
                locals,
                path,
                statement.runnable,
                items,
                limit=statement.lanes,
            )

    step = par_step if statement.runnable is not None else value_step
    return await step.execute(
        execution.emit,
        begin_step=execution.begin_step,
        binding=binding,
        path=path,
        statement=statement,
        locals=locals,
        controls=controls,
        occurrence=occurrence,
        evaluate=evaluate,
    )
