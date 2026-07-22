"""Scatter-statement semantics."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING

from toolang.common.errors import ToolangError
from toolang.lang.ast import ScatterStmt

from ...records import RunControlRecord, StepPath
from ..common import BoundRun
from ..common import Local, result_list
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
    placement: Mapping[str, object] | None,
) -> Local:
    def transform(result: Local) -> Local:
        values = result_list(result, operation="scatter")
        if len(values) != statement.count:
            raise ToolangError(
                f"scatter expected {statement.count} items, got {len(values)}"
            )
        return Local(values, "list")

    return await run_step.execute(
        execution,
        binding=binding,
        path=path,
        statement=statement,
        locals=locals,
        controls=controls,
        placement=placement,
        runnable=statement.runnable,
        transform=transform,
    )
