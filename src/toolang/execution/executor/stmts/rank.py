"""Rank-statement semantics."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING

from toolang.lang.ast import RankStmt

from ...records import RunControlRecord, StepPath
from ...types import Local as RecordLocal
from ..common import BoundRun
from ..common import Local, number, require_list, result_list
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
        source = locals.get("_", Local())
        input_type = source.type_name
        items = require_list(locals, operation="rank")
        evaluated = await execution.parallel_children(
            binding,
            locals,
            path,
            statement.scorer,
            items,
            limit=statement.par,
        )
        scores = [
            number(value, operation="rank")
            for value in result_list(evaluated, operation="rank")
        ]
        ranked_entries = sorted(
            zip(scores, items, range(len(items)), strict=True),
            key=lambda entry: (-entry[0], entry[2]),
        )
        if statement.limit == "top":
            ranked_entries = ranked_entries[: statement.count or 0]
        elif statement.limit == "bottom":
            count = statement.count or 0
            ranked_entries = ranked_entries[-count:] if count else []
        ranked = [item for _, item, _ in ranked_entries]
        ranked_indexes = [index for _, _, index in ranked_entries]
        return Local(
            ranked,
            "list",
            type_name=input_type,
            record=(
                RecordLocal(
                    type=f"{input_type or 'Json'}[]",
                    value=tuple(source.ref.select(index) for index in ranked_indexes),
                    dim=1,
                )
                if source.ref is not None
                else None
            ),
        )

    return await par_step.execute(
        execution.emit,
        binding=binding,
        path=path,
        statement=statement,
        locals=locals,
        controls=controls,
        placement=placement,
        operation=operation,
    )
