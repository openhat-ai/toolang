"""Native same-Run replacement and repeat unwinding."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING

from toolang.lang.ast import ExecStmt

from ...events import StepEnd
from ...records import ControlRecord, StepRef
from ...types import LoopStepNoted, Occurrence
from ..common import BoundRun, Local, _ExecuteCommitted, execute_step

if TYPE_CHECKING:
    from ..executor import _Execution


async def execute(
    execution: _Execution,
    binding: BoundRun,
    locals: Mapping[str, Local],
    path: StepRef,
    statement: ExecStmt,
    controls: Sequence[ControlRecord],
    occurrence: Occurrence | None,
) -> Local:
    async def evaluate() -> Local:
        successor, runnable, inputs = execution.prepare_flow_exec(
            binding, statement, locals
        )
        loops = []
        parent = path.parent
        while parent is not None:
            progress = execution.repeat_progress[parent]
            loops.append(
                (parent, LoopStepNoted(progress.iterations, "exec", progress.total))
            )
            parent = parent.parent
        successor = execution.commit_execute(successor, triggered_by=path, loops=loops)
        transfer = _ExecuteCommitted(successor, runnable, inputs)
        # All records are already terminal. Delivery can never turn an applied
        # handoff into a failed old Step or resume its repeat body.
        for ref in (path, *(ref for ref, _ in loops)):
            try:
                record = execution.store.get_step(ref=ref)
                assert record is not None and record.finished_at is not None
                await execution.emit(
                    StepEnd(
                        step=ref,
                        kind=record.kind,
                        status=record.status,
                        noted=record.noted,
                        aborted_by=record.aborted_by,
                        finished_at=record.finished_at,
                    )
                )
            except asyncio.CancelledError as exc:
                transfer.interruption = exc
            except Exception:
                logging.getLogger(__name__).exception(
                    "committed exec Step delivery failed: %s", ref
                )
        raise transfer

    return await execute_step(
        execution.emit,
        begin_step=execution.step_starter(binding),
        kind="exec",
        path=path,
        binding=binding,
        statement=statement,
        locals=locals,
        controls=controls,
        occurrence=occurrence,
        evaluate=evaluate,
        inputs=tuple(local.ref for local in locals.values() if local.ref is not None),
    )
