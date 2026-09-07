"""Per-call runtime authority; plugins never own Steps or execution transfer."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from toolang.base.errors import ToolFailure, ToolangError
from toolang.base.protocols.tool import ToolRuntime
from toolang.state.state import StatePublication

from ..runnables import (
    AgicRoutes,
    ResolvedRunnable,
    parse_runnable_ref,
    resolve_public_runnable,
)
from ..types import (
    ControlRef,
    ErrorMessage,
    ErrorRef,
    FieldRef,
    Local,
    RunRef,
    StepRef,
    TypedRef,
    local_to_protocol_data,
)
from .common import _ExecuteCommitted, _ExecutionFailed, _RunRejected

if TYPE_CHECKING:
    from .runs.agic import _AgicState


@dataclass(slots=True)
class _ToolRuntime(ToolRuntime):
    state: _AgicState
    step: StepRef
    source: FieldRef | None
    tool_call_count: int
    routes: AgicRoutes
    transfer: _ExecuteCommitted | None = None
    error: ErrorMessage | ErrorRef | None = None
    failure: Exception | None = None

    async def reload(self) -> dict[str, Any]:
        execution = self.state.execution
        if execution is None:
            raise RuntimeError("Agic runtime execution is unavailable")
        return await execution.executor.model_reload(
            run_id=self.step.run_id, triggered_by=self.step
        )

    async def run(self, runnable: str, input: Mapping[str, Any]) -> dict[str, Any]:
        state = self.state
        execution = state.execution
        if execution is None:
            raise RuntimeError("Agic runtime execution is unavailable")
        try:
            result = await execution.execute_child(
                state.prepared.run,
                {},
                self.step,
                runnable,
                None,
                resolution="state",
                raw_input=input,
                authorize=lambda target: self._authorize("run", target),
                state_snapshot=execution.state_for_step(self.step),
            )
        except (_RunRejected, _ExecutionFailed) as exc:
            if isinstance(exc, _ExecutionFailed):
                self.error = exc.error
            raise ToolFailure(
                str(exc), output=exc.details if isinstance(exc, _RunRejected) else {}
            ) from exc
        except Exception as exc:
            self.failure = exc
            raise
        record = result.record
        target = record.value if record is not None else None
        if (
            record is None
            or not isinstance(target, TypedRef)
            or not isinstance(target.ref.record, RunRef)
            or target.ref.tokens != ("output", "value")
        ):
            raise RuntimeError("runtime run result is missing its child run reference")
        state.output = target.ref
        state.record_output(target.ref)
        return {
            "run_id": str(target.ref.record),
            "output_type": record.type,
            "output": local_to_protocol_data(
                Local.typed(
                    record.type, result.value, dim=1 if result.shape == "list" else 0
                )
            )["value"],
        }

    async def execute(self, runnable: str, input: Mapping[str, Any]) -> dict[str, Any]:
        if self.source is None:
            raise ToolangError("execute requires a model ToolCall source")
        if self.tool_call_count != 1:
            raise ToolangError(
                "_toolang/execute must be the only tool call in its Model Call"
            )
        state = self.state
        execution = state.execution
        if execution is None:
            raise RuntimeError("Agic runtime execution is unavailable")
        captured, state_ref = execution.state_for_step(self.step)
        name, kind = parse_runnable_ref(runnable)
        target = resolve_public_runnable(
            captured.state if isinstance(captured, StatePublication) else captured,
            name,
            kind=kind,
        )
        self._authorize("execute", target)
        try:
            values = execution.resolve_public_input(
                captured, target.module, target.name, target.executable, input
            )
            binding, locals = execution.prepare_execute(
                state.prepared.run,
                target,
                values,
                source=self.source,
                state=captured,
                state_ref=state_ref,
            )
            committed = execution.commit_execute(binding, triggered_by=self.step)
        except _RunRejected as exc:
            raise ToolFailure(str(exc), output=exc.details) from exc
        except (ToolangError, TypeError, ValueError):
            raise
        except Exception as exc:
            self.failure = exc
            raise
        self.transfer = _ExecuteCommitted(committed, target.executable, locals)
        return {
            "controls": [
                str(ControlRef(RunRef(committed.run_id), committed.control_index))
            ]
        }

    def _authorize(
        self, operation: Literal["run", "execute"], target: ResolvedRunnable
    ) -> None:
        if not self.routes.allows(operation, target):
            selector = "hands" if operation == "run" else "handoffs"
            raise ToolangError(
                f"runnable is not authorized by {selector}: {target.ref}"
            )
