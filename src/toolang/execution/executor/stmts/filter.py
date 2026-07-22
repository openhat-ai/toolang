"""Keep- and drop-statement semantics."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING

from toolang.common.errors import ToolangError
from toolang.lang.ast import DropStmt, KeepStmt

from ...records import RunControlRecord, StepPath
from ..common import BoundRun
from ..common import Local, boolean, require_list
from ..steps import par as par_step
from ..steps import system as system_step

if TYPE_CHECKING:
    from ..executor import _Execution


async def execute(
    execution: _Execution,
    binding: BoundRun,
    locals: Mapping[str, Local],
    path: StepPath,
    statement: KeepStmt | DropStmt,
    controls: Sequence[RunControlRecord],
    placement: Mapping[str, object] | None,
) -> Local:
    async def operation() -> Local:
        items = require_list(locals, operation=statement.kind)
        if statement.position is not None:
            count = statement.count or 0
            selected = set(
                range(min(count, len(items)))
                if statement.position == "first"
                else range(max(len(items) - count, 0), len(items))
            )
            matches = [index in selected for index in range(len(items))]
        else:
            if statement.predicate is None:
                raise ToolangError(f"{statement.kind} requires a predicate")
            values = await execution.parallel_children(
                binding,
                locals,
                path,
                statement.predicate,
                items,
                limit=statement.par,
            )
            matches = [boolean(value, operation=statement.kind) for value in values]
        kept = [
            item
            for item, matched in zip(items, matches, strict=True)
            if matched == isinstance(statement, KeepStmt)
        ]
        return Local(kept, "list")

    step = par_step if statement.predicate is not None else system_step
    return await step.execute(
        execution.emit,
        path=path,
        statement=statement,
        locals=locals,
        controls=controls,
        placement=placement,
        operation=operation,
    )
