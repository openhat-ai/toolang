"""Literal let-statement semantics."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING

from toolang.lang.ast import LetStmt

from ...records import RunControlRecord, StepPath
from ..common import BoundRun
from ..common import Local
from ..steps import system as system_step

if TYPE_CHECKING:
    from ..executor import _Execution


async def execute(
    execution: _Execution,
    binding: BoundRun,
    locals: Mapping[str, Local],
    path: StepPath,
    statement: LetStmt,
    controls: Sequence[RunControlRecord],
    placement: Mapping[str, object] | None,
) -> Local:
    del binding

    async def operation() -> Local:
        return Local(statement.value, "item")

    return await system_step.execute(
        execution.emit,
        path=path,
        statement=statement,
        locals=locals,
        controls=controls,
        placement=placement,
        operation=operation,
    )
