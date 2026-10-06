"""One blocking Step observing a retained awaitable handle."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING

from toolang.common.errors import ToolangError
from toolang.lang.ast import AwaitStmt

from ...records import ControlRecord
from ...types import AwaitableHandle, FieldRef, Occurrence, RunRef, StepRef
from ..awaitables import wait, result
from ..common import BoundRun, Local, execute_step

if TYPE_CHECKING:
    from ..executor import _Execution


async def execute(
    execution: _Execution,
    binding: BoundRun,
    locals: Mapping[str, Local],
    path: StepRef,
    statement: AwaitStmt,
    controls: Sequence[ControlRecord],
    occurrence: Occurrence | None,
) -> Local:
    target = locals.get(statement.handle, Local())
    handle = target.value

    async def evaluate() -> Local:
        if not isinstance(handle, AwaitableHandle):
            raise ToolangError(f"await requires a retained handle: {statement.handle}")
        return result(execution, await wait(execution, binding, handle))

    # The target identity is inspectable at entry, even while output is pending.
    inputs = (
        [FieldRef.from_path(RunRef(handle.id), "id")]
        if isinstance(handle, AwaitableHandle)
        else []
    )
    if target.ref is not None:
        inputs.append(target.ref)
    return await execute_step(
        execution.emit,
        begin_step=execution.step_starter(binding),
        kind="value",
        path=path,
        binding=binding,
        statement=statement,
        locals=locals,
        controls=controls,
        occurrence=occurrence,
        evaluate=evaluate,
        inputs=inputs,
    )
