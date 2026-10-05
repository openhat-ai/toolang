"""Flow-statement dispatch."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from toolang.common.errors import ToolangError
from toolang.lang.ast import (
    AskStmt,
    DropStmt,
    FlowStmt,
    KeepStmt,
    LetStmt,
    MapStmt,
    SortStmt,
    RepeatStmt,
    RunStmt,
    ExecStmt,
    SeekStmt,
    ReduceStmt,
    GenerateStmt,
)

from ...records import ControlRecord, StepRef
from ...types import Occurrence
from ..common import BoundRun
from ..common import Local
from . import (
    ask,
    filter,
    let,
    map,
    sort,
    repeat,
    run,
    exec as exec_stmt,
    seek,
    reduce,
    generate,
)

if TYPE_CHECKING:
    from ..executor import _Execution


async def execute(
    execution: _Execution,
    binding: BoundRun,
    locals: dict[str, Local],
    *,
    path: StepRef,
    statement: FlowStmt,
    controls: Sequence[ControlRecord],
    occurrence: Occurrence | None,
) -> Local:
    """Dispatch one lowered flow statement to its semantic implementation."""

    if isinstance(statement, ExecStmt):
        return await exec_stmt.execute(
            execution, binding, locals, path, statement, controls, occurrence
        )
    if isinstance(statement, RunStmt):
        return await run.execute(
            execution, binding, locals, path, statement, controls, occurrence
        )
    if isinstance(statement, SeekStmt):
        return await seek.execute(
            execution, binding, locals, path, statement, controls, occurrence
        )
    if isinstance(statement, AskStmt):
        return await ask.execute(
            execution, binding, locals, path, statement, controls, occurrence
        )
    if isinstance(statement, GenerateStmt):
        return await generate.execute(
            execution, binding, locals, path, statement, controls, occurrence
        )
    if isinstance(statement, ReduceStmt):
        return await reduce.execute(
            execution, binding, locals, path, statement, controls, occurrence
        )
    if isinstance(statement, MapStmt):
        return await map.execute(
            execution, binding, locals, path, statement, controls, occurrence
        )
    if isinstance(statement, KeepStmt | DropStmt):
        return await filter.execute(
            execution, binding, locals, path, statement, controls, occurrence
        )
    if isinstance(statement, SortStmt):
        return await sort.execute(
            execution, binding, locals, path, statement, controls, occurrence
        )
    if isinstance(statement, RepeatStmt):
        return await repeat.execute(
            execution, binding, locals, path, statement, controls, occurrence
        )
    if isinstance(statement, LetStmt):
        return await let.execute(
            execution, binding, locals, path, statement, controls, occurrence
        )
    raise ToolangError(f"unsupported flow statement: {statement.kind}")
