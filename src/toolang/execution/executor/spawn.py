"""Independent root admission shared by flow and model tools."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import replace
from typing import TYPE_CHECKING, Literal, cast

from toolang.common.errors import ToolangError
from toolang.common.template import template_runtime_names
from toolang.lang.ast import (
    AgicDecl,
    FlowDecl,
    RepeatStmt,
    ReduceStmt,
    LetStmt,
    AskStmt,
    SeekStmt,
    GenerateStmt,
    SpawnStmt,
)
from toolang.lang.contracts import OutputContract
from toolang.state.state import AgentState, state_program

from ..records import LaunchContext, RunControlPayload
from ..events import ThreadCreated
from ..runnables import ResolvedRunnable, resolve_call_target
from ..settings import resolve_settings
from ..types import (
    ControlRef,
    AwaitableHandle,
    RunRef,
    StepRef,
    ThreadKind,
    RunnableSettings,
    ToolStepSpec,
)
from .common import BoundRun, Local, _RunRejected

if TYPE_CHECKING:
    from .executor import _Execution


async def accept(
    execution: _Execution,
    parent: BoundRun,
    locals: Mapping[str, Local],
    step: StepRef,
    name: str,
    *,
    resolution: Literal["module", "state"] = "module",
    raw_input: Mapping[str, object] | None = None,
    authorize: Callable[[ResolvedRunnable], None] | None = None,
    state_snapshot: tuple[AgentState, ControlRef] | None = None,
    thread: str | None = None,
) -> AwaitableHandle:
    from .executor import (
        _bound_runnable,
        _child_binding,
        _setup_sandbox,
    )

    def admit() -> AwaitableHandle:
        executor = execution.executor
        executor._require_available()
        try:
            previous = execution.store.get_spawn_receipt(step)
        except ValueError as exc:
            raise ToolangError(str(exc)) from exc
        if previous is not None:
            payload, handle = previous
            _validate_replay(
                execution,
                parent,
                locals,
                step,
                name,
                payload=payload,
                resolution=resolution,
                raw_input=raw_input,
                thread=thread,
            )
            return handle
        state, state_ref = state_snapshot or (parent.state, parent.state_ref)
        try:
            state, target = execution.resolve_invocation(
                parent,
                name,
                baseline_state=state if resolution == "state" else parent.state,
                authorize=authorize,
                action="spawn",
            )
            runnable = target.executable
            if resolution == "state":
                values = execution.resolve_public_input(
                    state,
                    target.module,
                    target.name,
                    runnable,
                    raw_input or {},
                )
                bound = execution._prepare_public_child(
                    parent,
                    target.module,
                    target.name,
                    runnable,
                    values,
                    parent_step=step,
                    state=state,
                    state_ref=state_ref,
                    ordinary_defaults=True,
                )
            else:
                bound = _child_binding(
                    execution,
                    parent,
                    target.module,
                    target.name,
                    runnable,
                    locals,
                    parent_step=step,
                    occurrence=None,
                    state=state,
                    state_ref=state_ref,
                )
                bound = execution.prepare_resources(
                    bound, runnable, ordinary_defaults=True
                )
            iterations = execution.iteration_values(parent, step=step)
            for template in _outer_templates(
                state, bound.module, runnable, bound.settings
            ):
                for ref in template_runtime_names(template):
                    if (
                        ref.startswith("_")
                        and ref[1:].isdigit()
                        and iterations.get(ref) is None
                    ):
                        raise ToolangError(
                            f"spawn cannot capture unavailable iteration input: {ref}"
                        )
            resources = bound.resources
            assert resources is not None
            target_thread = thread or executor.ids.issue_thread(ThreadKind.WORK.value)
            if (
                thread is not None
                and executor.store.get_thread(thread_id=thread) is None
            ):
                raise ValueError(f"spawn thread not found: {thread}")
            from ..inspection.history import RunHistory

            compaction = (
                RunHistory(executor.store).get_compaction(target_thread)
                if thread is not None
                else None
            )
            bound = replace(
                bound,
                root_run_id=bound.run_id,
                thread=target_thread,
                parent=None,
                call="top",
                occurrence=None,
                horizon=compaction.ref if compaction is not None else None,
                conversation=None,
                cwd=execution.cwd_for_run(parent.run_id),
                state_ref=ControlRef(RunRef(bound.run_id), 0),
                resource_ceiling=resources,
                captured_iterations=iterations,
            )
            output_type = runnable.output or (
                "Part[]" if isinstance(runnable, AgicDecl) else None
            )
            handle = AwaitableHandle(bound.run_id, target_thread, output_type)
            result_contract = (
                OutputContract.resolve(
                    output_type,
                    structs={
                        s.name: s for s in state_program(state, target.module).structs
                    },
                )
                if output_type is not None
                else None
            )
        except (ToolangError, TypeError, ValueError) as exc:
            raise _RunRejected(str(exc) or type(exc).__name__) from exc

        # There is no suspension between the availability check, transaction,
        # and registration. stop() cannot miss an admitted root on this loop.
        with executor.stream.publication() as publication:
            with executor.store.write_transaction():
                handle, created = execution.store.accept_spawn(
                    handle=handle,
                    source=step,
                    resources=resources,
                    limits=bound.limits,
                    state=state.revision,
                    runnable=_bound_runnable(bound),
                    runnable_kind=bound.runnable_kind,
                    model_request=bound.model_request,
                    input=bound.control_input,
                    sandbox=_setup_sandbox(bound.setup),
                    cwd=bound.cwd,
                    created_at=bound.created_at,
                    tool_report=execution.tool_admission_report(step),
                    context=LaunchContext(
                        bound.settings,
                        bound.workspaces,
                        bound.captured_iterations,
                        result_contract,
                    ),
                    requested_thread=thread,
                    horizon=bound.horizon,
                )
                if created:
                    if thread is None:
                        event = ThreadCreated(
                            thread=handle.thread,
                            control=ControlRef.for_thread(handle.thread, 0),
                            created_at=bound.created_at,
                        )
                        executor.store.record_event_cursor(
                            event.control, publication.cursor
                        )
                        publication.append(event)
                    completed_steps = executor._publish_step_ends(
                        publication,
                        (step,),
                        thread_id=parent.thread,
                        root_run_id=parent.root_run_id,
                    )
            if created and execution._root_run is not None:
                for completed in completed_steps:
                    execution._root_run.tree.end_step(completed)
        if created:
            if execution._root_run is not None:
                execution._early_ends.add(step)
            try:
                executor._launch(
                    bound,
                    runnable,
                    tracer=None,
                    independent=True,
                )
            except Exception:
                record = executor.store.get_run(run_id=handle.id)
                if record is None or record.status != "failed":
                    raise
        return handle

    if execution._root_run is None:
        handle = admit()
    else:
        async with execution._root_run.boundary_lock:
            handle = admit()
    return handle


def _validate_replay(
    execution: _Execution,
    parent: BoundRun,
    locals: Mapping[str, Local],
    step: StepRef,
    name: str,
    *,
    payload: RunControlPayload,
    resolution: Literal["module", "state"],
    raw_input: Mapping[str, object] | None,
    thread: str | None,
) -> None:
    """Verify the source binding without discovering code or selecting policy."""

    recorded = execution.store.get_step(ref=step)
    if recorded is None or payload.spawn_thread != thread:
        raise ToolangError(f"conflicting spawn admission: {step}")
    if resolution == "state":
        if (
            not isinstance(recorded.spec, ToolStepSpec)
            or recorded.spec.call.name != "runtime__spawn"
            or recorded.spec.trigger != "model"
            or recorded.spec.call.input.get("runnable") != name
            or recorded.spec.call.input.get("input", {}) != (raw_input or {})
            or recorded.spec.call.input.get("thread") != thread
        ):
            raise ToolangError(f"conflicting spawn admission: {step}")
        return
    if not isinstance(recorded.spec, SpawnStmt) or recorded.spec.runnable != name:
        raise ToolangError(f"conflicting spawn admission: {step}")
    try:
        # The accepted caller pins the original contract and handle status.
        target = resolve_call_target(parent.state, parent.module, name)
        replay_locals = dict(locals)
        for key, value in payload.input.items():
            local = locals.get(key)
            if (
                local is None
                or not isinstance(local.value, AwaitableHandle)
                or not isinstance(value, Mapping)
            ):
                continue
            snapshot = cast(Mapping[str, object], value)
            if (
                snapshot.get("id") == local.value.id
                and snapshot.get("thread") == local.value.thread
            ):
                replay_locals[key] = Local(value=value, type_name="Json")
        _, replay_input = execution.bind_call_input(
            replay_locals,
            target.executable,
            name,
            state=parent.state,
            module=target.module,
            step=step,
        )
    except (ToolangError, TypeError, ValueError) as exc:
        raise ToolangError(f"conflicting spawn input: {step}") from exc
    if replay_input != payload.input:
        raise ToolangError(f"conflicting spawn input: {step}")


def _outer_templates(
    state: AgentState,
    module: str,
    runnable: AgicDecl | FlowDecl,
    settings: RunnableSettings,
) -> list[str]:
    """Find dependencies outside iteration scopes owned by the spawned root."""
    templates: list[str] = []
    visited: set[tuple[str, int]] = set()

    def visit(
        module: str, runnable: AgicDecl | FlowDecl, settings: RunnableSettings
    ) -> None:
        identity = (module, id(runnable))
        if identity in visited:
            return
        visited.add(identity)
        if isinstance(runnable, AgicDecl):
            templates.extend(message.content for message in runnable.messages)
            for kind, setting in (
                ("instruct", settings.instruct),
                ("context", settings.context),
            ):
                if setting is None or setting.name == "none":
                    continue
                if setting.name == "default" and setting.module not in state.modules:
                    # Default prompts need no declaration in the published State.
                    continue
                program = state_program(state, setting.module)
                declaration = (
                    program.find_instruct(setting.name)
                    if kind == "instruct"
                    else program.find_context(setting.name)
                )
                if declaration is not None:
                    templates.append(declaration.body)
            return
        for statement in runnable.stmts:
            if isinstance(statement, LetStmt):
                templates.append(statement.value)
            if isinstance(statement, AskStmt):
                templates.append(statement.request)
            if isinstance(statement, ReduceStmt) and statement.initial is not None:
                templates.append(statement.initial)
            if isinstance(statement, RepeatStmt | ReduceStmt | SeekStmt) or (
                isinstance(statement, GenerateStmt) and statement.count == 0
            ):
                continue
            reference = getattr(statement, "runnable", None)
            if reference is not None:
                target = resolve_call_target(state, module, reference)
                visit(
                    target.module,
                    target.executable,
                    resolve_settings(target.executable, target.module, settings),
                )

    visit(module, runnable, settings)
    return templates
