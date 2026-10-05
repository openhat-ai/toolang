"""Per-call runtime authority; plugins never own Steps or execution transfer."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from toolang.base.errors import ToolangError
from toolang.base.protocols.tool import ToolRuntime
from toolang.base.types.tool import ToolContext, ToolResult
from toolang.base.types.message import ToolResultPart
from toolang.base.utils.workspace_paths import resolve_input_path, workspace_uri
from toolang.lang.input import CallInput

from ..records import RecallControlPayload
from ..assembly.tool_replies import control_summary
from ..assembly.run_results import run_receipt

from ..runnables import (
    AgicRoutes,
    ResolvedRunnable,
)
from ..types import (
    ControlRef,
    ErrorMessage,
    ErrorRef,
    FieldRef,
    RunRef,
    SkillRecallTarget,
    ServiceRecallTarget,
    StepRef,
    TypedRef,
    WorkspaceRecallTarget,
)
from .common import _ExecuteCommitted, _RunRejected
from ..recall import required_declarations
from .resources import resource_caps, workspace_declarations
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

    async def chdir(self, path: str, context: ToolContext) -> ToolResult:
        if self.tool_call_count != 1:
            raise ToolangError(
                "_toolang/chdir must be the only tool call in its Model Call"
            )
        target, name, relative = resolve_input_path(path, context)
        if not target.is_dir():
            raise ToolangError(f"chdir target is not a directory: {path}")
        return ToolResult({"cwd": workspace_uri(name, relative)})

    async def compact(self) -> ToolResult:
        from .runs.compact import invoke

        return ToolResult(await invoke(self.state, self.step))

    async def pick(self, kind: Literal["skill", "service"], ref: str) -> ToolResult:
        execution = self.state.execution
        if execution is None:
            raise RuntimeError("Agic runtime execution is unavailable")
        frame = self.state.prepared
        resources = frame.run.resources
        if resources is None:
            raise RuntimeError(f"run resources missing: {self.step.run_id}")
        cap = next(
            (
                cap
                for cap in resource_caps(
                    frame.state, resources, module=frame.run.module
                )
                if cap.kind == kind and cap.effective_ref == ref
            ),
            None,
        )
        if cap is None:
            raise ToolangError(f"{kind} is not available: {ref}")
        content = cap.read_content()
        payload = RecallControlPayload(
            SkillRecallTarget(ref) if kind == "skill" else ServiceRecallTarget(ref),
            cap.revision,
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
        captured = self.state.prepared.run
        context = ToolContext(
            home=self.state.layout.home,
            room=self.state.layout.tool_room("_toolang"),
            workspaces=self.state.prepared.run.setup.workspace_roots(
                captured.workspaces
            ),
            workspace_names=tuple(
                self.state.prepared.run.setup.workspace_grants(captured.workspaces)
            ),
            workspace_bindings=self.state.prepared.run.setup.workspace_grants(
                captured.workspaces
            ),
            cwd=execution.cwd_for_run(self.step.run_id),
        )
        pending = execution.runtime_controls(self.step.run_id)
        visible = dict(self.state.visible_recalls)
        visible.update(
            (c.payload.target, c.payload.revision)
            for c in pending
            if isinstance(c.payload, RecallControlPayload)
        )
        # Reconcile inherited rule declarations with this Run's bound workspaces.
        for payload in required_declarations(
            workspace_declarations(
                self.state.prepared.run.setup.workspace_grants(captured.workspaces)
            ),
            {
                target: revision
                for target, revision in visible.items()
                if target.kind in {"workspace", "rules"}
            },
        ):
            execution.recall(self.step, payload, self.state.visible_recalls)
        self.state.visible_recalls.update(
            (item.target, item.revision)
            for item in workspace_declarations(
                self.state.prepared.run.setup.workspace_grants(captured.workspaces)
            )
            if isinstance(item.target, WorkspaceRecallTarget)
            and item.target.ref in context.workspaces
        )
        summaries = {
            ref: control_summary(ref, payload)
            for payload in load_rules(context, paths, set(visible))
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
            binding, target = await execution.accept_child(
                state.prepared.run,
                {},
                self.step,
                runnable,
                None,
                resolution="state",
                raw_input=input,
                authorize=lambda target: self._authorize("run", target),
                state_snapshot=(
                    state.prepared.state,
                    state.prepared.run.state_ref,
                ),
            )
        except _RunRejected as exc:
            return ToolResult(error=str(exc), output=exc.details)
        except Exception as exc:
            self.failure = exc
            raise
        state.scheduled_run = (binding, target)
        return ToolResult(run_receipt(binding.run_id))

    async def exec(self, runnable: str, input: Mapping[str, Any]) -> ToolResult:
        if self.source is None:
            raise ToolangError("exec requires a model ToolCall source")
        if self.tool_call_count != 1:
            raise ToolangError(
                "_toolang/exec must be the only tool call in its Model Call"
            )
        state = self.state
        execution = state.execution
        if execution is None:
            raise RuntimeError("Agic runtime execution is unavailable")
        captured = state.prepared.state
        state_ref = state.prepared.run.state_ref
        captured, target = execution.resolve_invocation(
            state.prepared.run,
            runnable,
            baseline_state=captured,
            authorize=lambda target: self._authorize("exec", target),
            action="_toolang/exec",
        )
        try:
            values = execution.resolve_public_input(
                captured, target.module, target.name, target.executable, input
            )
            binding, locals = execution.prepare_execute(
                state.prepared.run,
                target,
                values,
                control_input=CallInput(
                    {
                        name: TypedRef(
                            self.source.select("input", "input", name), "Json"
                        )
                        for name in values
                    }
                ),
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
        self.transfer = _ExecuteCommitted(
            committed, target.executable, locals, triggered_by=self.step
        )
        return ToolResult(
            {
                "controls": [
                    str(ControlRef(RunRef(committed.run_id), committed.control_index))
                ]
            }
        )

    async def spawn(self, runnable: str, input: Mapping[str, Any]) -> ToolResult:
        from .spawn import accept

        execution = self.state.execution
        if execution is None:
            raise RuntimeError("Agic runtime execution is unavailable")
        try:
            await accept(
                execution,
                self.state.prepared.run,
                {},
                self.step,
                runnable,
                resolution="state",
                raw_input=input,
                authorize=lambda target: self._authorize("spawn", target),
                state_snapshot=(
                    self.state.prepared.state,
                    self.state.prepared.run.state_ref,
                ),
            )
        except _RunRejected as exc:
            return ToolResult(error=str(exc), output=exc.details)
        except Exception as exc:
            self.failure = exc
            raise
        recorded = execution.store.get_step(ref=self.step)
        if (
            recorded is None
            or recorded.output is None
            or not isinstance(recorded.output.value, ToolResultPart)
        ):
            raise RuntimeError("spawn admission has no recorded tool result")
        return ToolResult(dict(recorded.output.value.output))

    def _authorize(
        self, operation: Literal["run", "spawn", "exec"], target: ResolvedRunnable
    ) -> None:
        if not self.routes.allows(operation, target):
            selector = "hands" if operation in {"run", "spawn"} else "handoffs"
            raise ToolangError(
                f"runnable is not authorized by {selector}: {target.ref}"
            )
