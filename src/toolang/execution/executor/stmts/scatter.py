"""Scatter-statement semantics."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING

from toolang.lang.ast import ScatterStmt

from ...records import RunControlRecord, StepPath
from ...types import Occurrence
from ..common import BoundRun
from ..common import Local
from ..steps import run as run_step

if TYPE_CHECKING:
    from ..executor import _Execution


async def execute(
    execution: _Execution,
    binding: BoundRun,
    locals: Mapping[str, Local],
    path: StepPath,
    statement: ScatterStmt,
    controls: Sequence[RunControlRecord],
    occurrence: Occurrence | None,
) -> Local:
    return await run_step.execute(
        execution,
        binding=binding,
        path=path,
        statement=statement,
        locals=locals,
        controls=controls,
        occurrence=occurrence,
        runnable=statement.runnable,
    )
