"""Child-run statement steps."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import TYPE_CHECKING

from toolang.lang.ast import FlowStmt

from ...records import RunControlRecord, StepPath
from ...types import Occurrence
from ..common import BoundRun
from ..common import Local, execute_step

if TYPE_CHECKING:
    from ..executor import _Execution


async def execute(
    execution: _Execution,
    *,
    binding: BoundRun,
    path: StepPath,
    statement: FlowStmt,
    locals: Mapping[str, Local],
    controls: Sequence[RunControlRecord],
    occurrence: Occurrence | None,
    runnable: str,
    validate: Callable[[], None] | None = None,
) -> Local:
    """Evaluate one child-run Step and emit its event boundary."""

    async def evaluate() -> Local:
        if validate is not None:
            validate()
        return await execution.execute_child(
            binding,
            locals,
            path,
            runnable,
            occurrence,
        )

    return await execute_step(
        execution.emit,
        kind="run",
        path=path,
        binding=binding,
        statement=statement,
        locals=locals,
        controls=controls,
        occurrence=occurrence,
        evaluate=evaluate,
    )
