"""Seek-statement semantics."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING

from toolang.common.errors import ToolangError
from toolang.lang.ast import SeekStmt

from ...records import ControlRecord, StepPath
from ...types import Occurrence
from ..common import BoundRun
from ..common import Local
from ..steps import agent as agent_step

if TYPE_CHECKING:
    from ..executor import _Execution


async def execute(
    execution: _Execution,
    binding: BoundRun,
    locals: Mapping[str, Local],
    path: StepPath,
    statement: SeekStmt,
    controls: Sequence[ControlRecord],
    occurrence: Occurrence | None,
) -> Local:
    async def evaluate() -> Local:
        raise ToolangError("seek requires an agent execution bridge")

    return await agent_step.execute(
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
