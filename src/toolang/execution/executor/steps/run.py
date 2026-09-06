"""Child-run statement steps."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import TYPE_CHECKING

from toolang.lang.ast import FlowStmt

from ...records import ControlRecord
from ...types import Occurrence, StepRef
from ..common import BoundRun
from ..common import Local, _RunRejected, execute_step

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
    runnable: str,
    validate: Callable[[], None] | None = None,
) -> Local:
    """Evaluate one child-run Step and emit its event boundary."""

    async def evaluate() -> Local:
        if validate is not None:
            try:
                validate()
            except (TypeError, ValueError) as exc:
                raise _RunRejected(str(exc) or type(exc).__name__) from exc
        return await execution.execute_child(
            binding,
            locals,
            path,
            runnable,
            occurrence,
            state_snapshot=execution.state_for_step(path),
        )

    return await execute_step(
        execution.emit,
        begin_step=execution.begin_step,
        kind="run",
        path=path,
        binding=binding,
        statement=statement,
        locals=locals,
        controls=controls,
        occurrence=occurrence,
        evaluate=evaluate,
    )
