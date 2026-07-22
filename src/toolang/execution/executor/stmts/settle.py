"""Settle-statement semantics."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING

from toolang.lang.ast import SettleStmt

from ...records import RunControlRecord, StepPath
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
    controls: Sequence[RunControlRecord],
    placement: Mapping[str, object] | None,
) -> Local:
    async def operation() -> Local:
        items = require_list(locals, operation="settle")
        accumulator = Local()
        for index, item in enumerate(items):
            child_locals = dict(locals)
            child_locals["_"] = Local(item, "item")
            child_locals["accumulator"] = accumulator
            accumulator = await execution.execute_child(
                binding,
                child_locals,
                path,
                statement.runnable,
                {"item": index, "items": len(items), "loop": index},
            )
        return accumulator

    return await loop_step.execute(
        execution.emit,
        path=path,
        statement=statement,
        locals=locals,
        controls=controls,
        placement=placement,
        operation=operation,
    )
