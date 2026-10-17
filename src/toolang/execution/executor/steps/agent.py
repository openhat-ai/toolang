"""Agent-interaction statement steps."""

from __future__ import annotations

from typing import TYPE_CHECKING

from collections.abc import Awaitable, Callable, Mapping, Sequence

from toolang.lang.ast import FlowStmt

from ...records import ControlRecord, StepRef
from ...types import Occurrence
from ..common import BoundRun, Local, execute_step


if TYPE_CHECKING:
    from ..executor import _Execution


async def execute(
    execution: _Execution,
    *,
    binding: BoundRun,
    path: StepRef,
    statement: FlowStmt,
    locals: Mapping[str, Local],
    controls: Sequence[ControlRecord],
    occurrence: Occurrence | None,
    evaluate: Callable[[], Awaitable[Local]],
) -> Local:
    """Execute one agent interaction and emit its step events."""

    return await execute_step(
        execution,
        kind="agent",
        path=path,
        binding=binding,
        statement=statement,
        locals=locals,
        controls=controls,
        occurrence=occurrence,
        evaluate=evaluate,
    )
