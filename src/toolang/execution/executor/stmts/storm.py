"""Storm-statement semantics."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING

from toolang.lang.ast import StormStmt

from ...records import RunControlRecord, StepPath
from ..common import BoundRun
from ..common import Local, require_item
from ..steps import par as par_step

if TYPE_CHECKING:
    from ..executor import _Execution


async def execute(
    execution: _Execution,
    binding: BoundRun,
    locals: Mapping[str, Local],
    path: StepPath,
    statement: StormStmt,
    controls: Sequence[RunControlRecord],
    placement: Mapping[str, object] | None,
) -> Local:
    async def operation() -> Local:
        basis = require_item(locals, operation="storm")
        return await execution.parallel_children(
            binding,
            locals,
            path,
            statement.runnable,
            [basis] * statement.count,
            limit=statement.par,
            select_source=False,
        )

    return await par_step.execute(
        execution.emit,
        path=path,
        statement=statement,
        locals=locals,
        controls=controls,
        placement=placement,
        operation=operation,
    )
