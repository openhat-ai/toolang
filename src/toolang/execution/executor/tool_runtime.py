"""Per-call runtime authority; plugins never own Steps or execution transfer."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from toolang.base.errors import ToolangError
from toolang.base.protocols.tool import ToolRuntime
from toolang.base.types.tool import ToolContext, ToolResult
from toolang.state.state import entry_ref

from ..records import RecallControlPayload
from ..tool_results import control_summary

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
    SkillRecallTarget,
    ServiceRecallTarget,
    StepRef,
    TypedRef,
    local_to_protocol_data,
)
from .common import _ExecuteCommitted, _ExecutionFailed, _RunRejected
from .resources import resource_caps
from .rules import load_rules

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

    async def reload(self) -> ToolResult:
        execution = self.state.execution
        if execution is None:
            raise RuntimeError("Agic runtime execution is unavailable")
        return await execution.executor.model_reload(
            run_id=self.step.run_id, triggered_by=self.step
        )

    async def compact(self, thread: str, begin: str | None, end: str) -> ToolResult:
        from .compact import execute

        return ToolResult(await execute(self.state, self.step, thread, begin, end))

    async def pick(self, kind: Literal["skill", "service"], ref: str) -> ToolResult:
        execution = self.state.execution
        if execution is None:
            raise RuntimeError("Agic runtime execution is unavailable")
        frame = self.state.frame_for_step(*execution.state_for_step(self.step))
        resources = frame.run.resources
        if resources is None:
            raise RuntimeError(f"run resources missing: {self.step.run_id}")
        cap = next(
            (
                cap
                for cap in resource_caps(
                    frame.run.state, resources, module=frame.run.module
                )
                if cap.kind == kind
                and entry_ref(cap, agent_name=self.state.layout.name) == ref
            ),
            None,
        )
        if cap is None:
            raise ToolangError(f"{kind} is not in the available catalog: {ref}")
        content = cap.read_content()
        payload = RecallControlPayload(
            SkillRecallTarget(ref) if kind == "skill" else ServiceRecallTarget(ref),
            sha256(content.encode("utf-8")).hexdigest(),
            content,
        )
        controls = execution.recall(self.step, payload, self.state.visible_recalls)
        return ToolResult(
            {"controls": [control_summary(ref, payload) for ref in controls]}
        )

    async def honor(self, paths: tuple[tuple[str, str], ...]) -> ToolResult:
        execution = self.state.execution
        if execution is None:
            raise RuntimeError("Agic runtime execution is unavailable")
        captured, _ref = execution.state_for_step(self.step)
        context = ToolContext(
            home=self.state.layout.home,
            room=self.state.layout.tool_room("_toolang"),
            workspaces={name: Path(path) for name, path in captured.workspaces.items()},
        )
        pending = execution.runtime_controls(self.step.run_id)
        known = set(self.state.visible_recalls) | {
            c.payload.target
            for c in pending
            if isinstance(c.payload, RecallControlPayload)
        }
        summaries = {
            ref: control_summary(ref, payload)
            for payload in load_rules(context, paths, known)
            for ref in execution.recall(self.step, payload, self.state.visible_recalls)
        }
        return ToolResult(
            {
                "controls": [
                    summaries[ref]
                    for ref in sorted(summaries, key=lambda ref: ref.index)
                ]
            }
        )

    async def run(self, runnable: str, input: Mapping[str, Any]) -> ToolResult:
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
            return ToolResult(
                error=str(exc),
                output=exc.details if isinstance(exc, _RunRejected) else {},
            )
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
        return ToolResult(
            {
                "run_id": str(target.ref.record),
                "output_type": record.type,
                "output": local_to_protocol_data(
                    Local.typed(
                        record.type,
                        result.value,
                        dim=1 if result.shape == "list" else 0,
                    )
                )["value"],
            }
        )

    async def execute(self, runnable: str, input: Mapping[str, Any]) -> ToolResult:
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
            captured,
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
            return ToolResult(error=str(exc), output=exc.details)
        except (ToolangError, TypeError, ValueError):
            raise
        except Exception as exc:
            self.failure = exc
            raise
        self.transfer = _ExecuteCommitted(committed, target.executable, locals)
        return ToolResult(
            {
                "controls": [
                    str(ControlRef(RunRef(committed.run_id), committed.control_index))
                ]
            }
        )

    def _authorize(
        self, operation: Literal["run", "execute"], target: ResolvedRunnable
    ) -> None:
        if not self.routes.allows(operation, target):
            selector = "hands" if operation == "run" else "handoffs"
            raise ToolangError(
                f"runnable is not authorized by {selector}: {target.ref}"
            )
