"""Flow-statement dispatch."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING

from toolang.common.errors import ToolangError
from toolang.lang.ast import (
    AskStmt,
    DropStmt,
    FlowStmt,
    GatherStmt,
    KeepStmt,
    LetStmt,
    MapStmt,
    RankStmt,
    RepeatStmt,
    RunStmt,
    ScatterStmt,
    SeekStmt,
    SettleStmt,
    StormStmt,
)

from ...records import RunControlRecord, StepPath
from ..common import BoundRun
from ..common import Local
from . import (
    ask,
    filter,
    gather,
    let,
    map,
    rank,
    repeat,
    run,
    scatter,
    seek,
    settle,
    storm,
)

if TYPE_CHECKING:
    from ..executor import _Execution


async def execute(
    execution: _Execution,
    binding: BoundRun,
    locals: Mapping[str, Local],
    *,
    path: StepPath,
    statement: FlowStmt,
    controls: Sequence[RunControlRecord],
    placement: Mapping[str, object] | None,
) -> Local:
    """Dispatch one lowered flow statement to its semantic implementation."""

    if isinstance(statement, RunStmt):
        return await run.execute(
            execution, binding, locals, path, statement, controls, placement
        )
    if isinstance(statement, SeekStmt):
        return await seek.execute(
            execution, binding, locals, path, statement, controls, placement
        )
    if isinstance(statement, AskStmt):
        return await ask.execute(
            execution, binding, locals, path, statement, controls, placement
        )
    if isinstance(statement, ScatterStmt):
        return await scatter.execute(
            execution, binding, locals, path, statement, controls, placement
        )
    if isinstance(statement, StormStmt):
        return await storm.execute(
            execution, binding, locals, path, statement, controls, placement
        )
    if isinstance(statement, GatherStmt):
        return await gather.execute(
            execution, binding, locals, path, statement, controls, placement
        )
    if isinstance(statement, SettleStmt):
        return await settle.execute(
            execution, binding, locals, path, statement, controls, placement
        )
    if isinstance(statement, MapStmt):
        return await map.execute(
            execution, binding, locals, path, statement, controls, placement
        )
    if isinstance(statement, KeepStmt | DropStmt):
        return await filter.execute(
            execution, binding, locals, path, statement, controls, placement
        )
    if isinstance(statement, RankStmt):
        return await rank.execute(
            execution, binding, locals, path, statement, controls, placement
        )
    if isinstance(statement, RepeatStmt):
        return await repeat.execute(
            execution, binding, locals, path, statement, controls, placement
        )
    if isinstance(statement, LetStmt):
        return await let.execute(
            execution, binding, locals, path, statement, controls, placement
        )
    raise ToolangError(f"unsupported flow statement: {statement.kind}")
