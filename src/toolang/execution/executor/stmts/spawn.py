"""Flow spawn admission, without waiting or an implicit result binding."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING

from toolang.lang.ast import SpawnStmt

from ...records import ControlRecord
from ...types import Occurrence, StepRef
from ..common import BoundRun, Local, execute_step
from ..spawn import accept

if TYPE_CHECKING:
    from ..executor import _Execution


async def execute(
    execution: _Execution,
    binding: BoundRun,
    locals: Mapping[str, Local],
    path: StepRef,
    statement: SpawnStmt,
    controls: Sequence[ControlRecord],
    occurrence: Occurrence | None,
) -> Local:
    async def evaluate() -> Local:
        handle = await accept(
            execution,
            binding,
            locals,
            path,
            statement.runnable,
            state_snapshot=execution.state_for_step(path),
        )
        return Local(value=handle, stored=handle)

    return await execute_step(
        execution.emit,
        begin_step=execution.step_starter(binding),
        kind="run",
        path=path,
        binding=binding,
        statement=statement,
        locals=locals,
        controls=controls,
        occurrence=occurrence,
        evaluate=evaluate,
    )
