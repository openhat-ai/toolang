"""Ask-statement semantics."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING

from toolang.common.errors import ToolangError
from toolang.lang.ast import AskStmt

from ...records import RunControlRecord, StepPath
from ..common import BoundRun
from ..common import Local
from ..steps import human as human_step

if TYPE_CHECKING:
    from ..executor import _Execution


async def execute(
    execution: _Execution,
    binding: BoundRun,
    locals: Mapping[str, Local],
    path: StepPath,
    statement: AskStmt,
    controls: Sequence[RunControlRecord],
    placement: Mapping[str, object] | None,
) -> Local:
    del binding

    async def operation() -> Local:
        raise ToolangError("ask requires a human input bridge")

    return await human_step.execute(
        execution.emit,
        path=path,
        statement=statement,
        locals=locals,
        controls=controls,
        placement=placement,
        operation=operation,
    )
