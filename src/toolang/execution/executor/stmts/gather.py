"""Gather-statement semantics."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING

from toolang.lang.ast import GatherStmt

from ...records import RunControlRecord, StepPath
from ..common import BoundRun
from ..common import Local, require_list
from ..steps import run as run_step

if TYPE_CHECKING:
    from ..executor import _Execution


async def execute(
    execution: _Execution,
    binding: BoundRun,
    locals: Mapping[str, Local],
    path: StepPath,
    statement: GatherStmt,
    controls: Sequence[RunControlRecord],
    placement: Mapping[str, object] | None,
) -> Local:
    def transform(result: Local) -> Local:
        return Local(result.value, "item", type_name=result.type_name)

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
        validate=lambda: require_list(locals, operation="gather"),
    )
