"""Per-call runtime authority; plugins never own Steps or execution transfer."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from toolang.base.errors import ToolangError
from toolang.base.protocols.tool import ToolRuntime
from toolang.base.types.tool import ToolContext, ToolResult
from toolang.base.types.message import ToolResultPart
from toolang.base.types.message import Message
from toolang.common.time import utc_now
from toolang.lang.input import CallInput
from toolang.lang.types import Array
from pydantic import TypeAdapter
from toolang.base.utils.workspace_paths import resolve_input_path, workspace_uri

from toolang.state.state import AgentState

from ..records import RecallControlPayload
from ..schemas import ControlInfo, RunDetail, record_to_data
from ..inspection.history import RunHistory
from ..assembly.tool_replies import control_summary
from ..assembly.run_results import run_result

from ..runnables import (
    AgicRoutes,
    ResolvedRunnable,
    bound_runnable,
    resolve_call_target,
    runnable_ref,
    runnable_signature,
    visible_runnables,
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
    WorkspaceRecallTarget,
    Output,
    ToolStepSpec,
    ToolStepReport,
)
from .common import BoundRun, _ExecuteCommitted, _RunRejected
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
    discovery_state: AgentState
    transfer: _ExecuteCommitted | None = None
    error: ErrorMessage | ErrorRef | None = None
    failure: Exception | None = None

    async def runnables(self, name: str | None = None) -> ToolResult:
        execution = self.state.execution
        if execution is None:
            raise RuntimeError("Agic runtime execution is unavailable")
        frame = self.state.prepared
        binding = frame.run

        def target_for(run: BoundRun) -> ResolvedRunnable:
            if run.bindings.runnable is None:
                raise RuntimeError(f"run runnable binding missing: {run.run_id}")
            return bound_runnable(run.state, run.module, run.bindings.runnable)

        current = target_for(binding)
        current_ref = runnable_ref(binding.state, current)
        if name is None:
            # The accepted current declaration stays pinned across publications.
            targets = {
                target.qualified: (self.discovery_state, target)
                for target in visible_runnables(self.discovery_state, binding.module)
            }
            targets[current.qualified] = (binding.state, current)
        else:
            try:
                pinned = resolve_call_target(binding.state, binding.module, name)
            except ToolangError:
                pinned = None
            if pinned is not None and pinned.qualified == current.qualified:
                targets = {current.qualified: (binding.state, current)}
            else:
                target = resolve_call_target(self.discovery_state, binding.module, name)
                targets = {target.qualified: (self.discovery_state, target)}
        entries = [
            {
                "ref": runnable_ref(state, target),
                "name": target.name,
                "kind": target.executable.kind,
                "full_name": target.qualified,
                "revision": state.revision,
                "doc": target.executable.doc or "",
                "signature": runnable_signature(
                    state,
                    target.module,
                    target.executable,
                    documentation_limit=None,
                ),
            }
            for state, target in targets.values()
        ]
        return ToolResult(
            {
                "current": current_ref,
                "ancestors": [
                    runnable_ref(run.state, target_for(run))
                    for run in execution.active_path(binding)[:-1]
                ],
                "runnables": sorted(entries, key=lambda item: item["ref"]),
            }
        )

    async def chdir(self, path: str, context: ToolContext) -> ToolResult:
        if self.tool_call_count != 1:
            raise ToolangError(
                "runtime/chdir must be the only tool call in its Model Call"
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

    async def honor(
        self, paths: tuple[tuple[str, str], ...], *, conversations: tuple[str, ...] = ()
    ) -> ToolResult:
        execution = self.state.execution
        if execution is None:
            raise RuntimeError("Agic runtime execution is unavailable")
        captured = self.state.prepared.run
        context = ToolContext(
            home=self.state.layout.agent_home,
            room=self.state.layout.tool_room("runtime"),
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
        if conversations:
            from .conversations import load_target, connection_for

            for target in dict.fromkeys(conversations):
                payload = (
                    await load_target(
                        self.state.layout,
                        target,
                        connection=connection_for(self.state.prepared.run.setup),
                        pending=True,
                    )
                ).recall
                for ref in execution.recall(
                    self.step, payload, self.state.visible_recalls
                ):
                    summaries[ref] = control_summary(ref, payload)
        return ToolResult(
            {
                "controls": [
                    summaries[ref]
                    for ref in sorted(summaries, key=lambda ref: ref.index)
                ]
            }
        )

    async def run(
        self, runnable: str, input: Mapping[str, Any], *, asynchronous: bool = False
    ) -> ToolResult:
        state = self.state
        execution = state.execution
        if execution is None:
            raise RuntimeError("Agic runtime execution is unavailable")
        binding = None
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
                asynchronous=asynchronous,
                state_snapshot=(
                    state.prepared.state,
                    state.prepared.run.state_ref,
                ),
            )
            if asynchronous:
                recorded = execution.store.get_step(ref=self.step)
                if (
                    recorded is None
                    or recorded.output is None
                    or not isinstance(recorded.output.value, ToolResultPart)
                ):
                    raise RuntimeError("async admission has no recorded tool result")
                return ToolResult(dict(recorded.output.value.output))
            await execution.execute(binding, target, output_binding=None, begun=True)
        except _RunRejected as exc:
            return ToolResult(error=str(exc), output=exc.details)
        except asyncio.CancelledError:
            # Admission delivers RunBegin under its lock. Cancellation during
            # that delivery closes the child before accept_child can return.
            child = (
                execution.store.get_run(run_id=binding.run_id)
                if binding is not None
                else next(
                    (
                        run
                        for run in execution.store.list_run_tree(
                            root_run_id=state.prepared.run.run_id
                        )
                        if run.parent == self.step
                    ),
                    None,
                )
            )
            if child is None or not execution.canceled_within(child.id):
                raise
        except Exception as exc:
            child = (
                execution.store.get_run(run_id=binding.run_id)
                if binding is not None
                else None
            )
            if child is None or child.status in {"pending", "running"}:
                self.failure = exc
                raise
        else:
            child = execution.store.get_run(run_id=binding.run_id)
        if child is None:
            raise RuntimeError(f"child Run disappeared for {self.step}")
        if child.status == "succeeded" and child.output is not None:
            state.output = FieldRef.from_path(RunRef(child.id), "output", "value")
            state.record_output(state.output)
        elif child.error is not None:
            self.error = ErrorRef(FieldRef.from_path(RunRef(child.id), "error"))
        return run_result(
            child, execution.store.resolve_value, execution.store.resolve_error
        )

    async def await_target(self, target: str) -> ToolResult:
        from .awaitables import resolve, wait

        execution = self.state.execution
        if execution is None:
            raise RuntimeError("Agic runtime execution is unavailable")
        caller = self.state.prepared.run
        child = await wait(execution, caller, resolve(execution, caller, target))
        if child.status == "succeeded" and child.output is not None:
            self.state.output = FieldRef.from_path(RunRef(child.id), "output", "value")
            self.state.record_output(self.state.output)
        elif child.error is not None:
            self.error = ErrorRef(FieldRef.from_path(RunRef(child.id), "error"))
        return run_result(
            child, execution.store.resolve_value, execution.store.resolve_error
        )

    async def exec(self, runnable: str, input: Mapping[str, Any]) -> ToolResult:
        if self.source is None:
            raise ToolangError("exec requires a model ToolCall source")
        if self.tool_call_count != 1:
            raise ToolangError(
                "runtime/exec must be the only tool call in its Model Call"
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
            action="runtime/exec",
        )
        try:
            values = execution.resolve_public_input(
                captured, target.module, target.name, target.executable, input
            )
            binding, locals = execution.prepare_execute(
                state.prepared.run,
                target,
                values,
                control_input=values,
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

    async def steer(self, target: str, input: str, expected_control: str) -> ToolResult:
        return self._control("steer", target, expected_control, input)

    async def cancel(
        self, target: str, expected_control: str, reason: str | None = None
    ) -> ToolResult:
        return self._control("cancel", target, expected_control, reason)

    def _control(
        self,
        kind: Literal["steer", "cancel"],
        target: str,
        expected_control: str,
        text: str | None,
    ) -> ToolResult:
        execution = self.state.execution
        if execution is None:
            raise RuntimeError("Agic runtime execution is unavailable")
        executor, store = execution.executor, execution.store
        try:
            run_id = str(RunRef.parse(target))
            expected = ControlRef.parse(expected_control)
            if expected.target != RunRef(run_id):
                raise ValueError("expected_control must identify the target Run")
            with executor.stream.publication() as publication:
                with store.write_transaction():
                    step = store.get_step(ref=self.step)
                    if (
                        step is None
                        or not isinstance(step.spec, ToolStepSpec)
                        or step.spec.trigger != "model"
                    ):
                        raise ValueError("control requires a model Tool Step")
                    arguments = step.spec.call.input
                    if (
                        arguments.get("target") != target
                        or arguments.get("expected_control") != expected_control
                        or arguments.get("input" if kind == "steer" else "reason")
                        != text
                        or step.spec.call.name != f"runtime__{kind}"
                    ):
                        raise ValueError("conflicting control admission")
                    if step.output is not None and isinstance(
                        step.output.value, ToolResultPart
                    ):
                        return ToolResult(
                            dict(step.output.value.output), step.output.value.error
                        )
                    if step.status != "running":
                        raise ValueError(
                            "control requires an unacknowledged running Step"
                        )
                    run = store.get_run(run_id=run_id)
                    if run is None:
                        raise ValueError(f"Run not found: {run_id}")
                    if run.status not in {"pending", "running"}:
                        return ToolResult(
                            {"run": record_to_data(run)},
                            error=f"Run {run_id} is {run.status}",
                        )
                    executor._require_available()
                    executor._require_owned_control(run_id, expected)
                    values = (
                        CallInput(
                            {
                                "_": Array(
                                    "Part[]", tuple(Message.user(text or "").parts)
                                )
                            }
                        )
                        if kind == "steer"
                        else CallInput({"_": text} if text is not None else {})
                    )
                    control = store.accept_run_control(
                        run_id=run_id,
                        kind=kind,
                        timing="next_step" if kind == "steer" else "immediate",
                        input=values,
                        expected_control=expected,
                        request_id=f"tool:{self.step}",
                        triggered_by=self.step,
                        created_at=utc_now(),
                    )
                    detail = RunHistory(store).get_run(run_id)
                    assert detail is not None
                    receipt = {
                        "run": TypeAdapter(RunDetail).dump_python(detail, mode="json"),
                        "command": TypeAdapter(ControlInfo).dump_python(
                            ControlInfo.from_record(run, control), mode="json"
                        ),
                    }
                    completed = store.finish_step(
                        ref=self.step,
                        kind="tool",
                        status="succeeded",
                        output=Output(
                            ToolResultPart(
                                tool_call_id=step.spec.call.tool_call_id,
                                call_id=step.spec.call.call_id,
                                tool_name=step.spec.call.name,
                                tool_family=step.spec.call.name,
                                output=receipt,
                            )
                        ),
                        report=ToolStepReport(
                            summary=f"Accepted {kind}: {run_id}", invoked=True
                        ),
                        error=None,
                        finished_at=utc_now(),
                    )
                    executor._publish_step_ends(
                        publication,
                        (completed,),
                        thread_id=self.state.prepared.run.thread,
                        root_run_id=self.state.prepared.run.root_run_id,
                    )
                if execution._root_run is not None:
                    execution._root_run.tree.end_step(completed)
            if execution._root_run is not None:
                execution._early_ends.add(self.step)
            executor._observe_control(control)
            return ToolResult(receipt)
        except (ToolangError, TypeError, ValueError) as exc:
            return ToolResult(error=str(exc))

    async def spawn(
        self, runnable: str, input: Mapping[str, Any], *, thread: str | None = None
    ) -> ToolResult:
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
                thread=thread,
            )
        except _RunRejected as exc:
            return ToolResult(error=str(exc), output=exc.details)
        except (ToolangError, TypeError, ValueError) as exc:
            if execution._root_run is not None and self.step in execution._early_ends:
                self.failure = exc
                raise
            return ToolResult(error=str(exc))
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
        return ToolResult(
            dict(recorded.output.value.output), recorded.output.value.error
        )

    def _authorize(
        self, operation: Literal["run", "spawn", "exec"], target: ResolvedRunnable
    ) -> None:
        if not self.routes.allows(operation, target):
            selector = "hands" if operation in {"run", "spawn"} else "handoffs"
            raise ToolangError(
                f"runnable is not authorized by {selector}: {target.ref}"
            )
