"""Rank-statement semantics."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING

from toolang.lang.ast import RankStmt

from ...records import RunControlRecord, StepPath
from ..common import BoundRun
from ..common import Local, number, require_list
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
    placement: Mapping[str, object] | None,
) -> Local:
    async def operation() -> Local:
        items = require_list(locals, operation="rank")
        values = await execution.parallel_children(
            binding,
            locals,
            path,
            statement.scorer,
            items,
            limit=statement.par,
        )
        scores = [number(value, operation="rank") for value in values]
        ranked = [
            item
            for _, item, _ in sorted(
                zip(scores, items, range(len(items)), strict=True),
                key=lambda entry: (-entry[0], entry[2]),
            )
        ]
        if statement.limit == "top":
            ranked = ranked[: statement.count or 0]
        elif statement.limit == "bottom":
            count = statement.count or 0
            ranked = ranked[-count:] if count else []
        return Local(ranked, "list")

    return await par_step.execute(
        execution.emit,
        path=path,
        statement=statement,
        locals=locals,
        controls=controls,
        placement=placement,
        operation=operation,
    )
