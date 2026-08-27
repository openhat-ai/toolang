"""Rank-statement semantics."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING

from toolang.lang.ast import RankStmt

from ...records import RunControlRecord, StepPath
from ...types import Occurrence
from ..common import BoundRun
from ..common import Local, require_list
from ..steps import par as par_step

if TYPE_CHECKING:
    from ..executor import _Execution


async def execute(
    execution: _Execution,
    binding: BoundRun,
    locals: Mapping[str, Local],
    path: StepPath,
    statement: RankStmt,
    controls: Sequence[RunControlRecord],
    occurrence: Occurrence | None,
) -> Local:
    async def evaluate() -> Local:
        items = require_list(locals, operation="rank")
        return await execution.parallel_children(
            binding,
            locals,
            path,
            statement.runnable,
            items,
            limit=statement.lanes,
        )

    return await par_step.execute(
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
