"""Independent root admission shared by flow and model tools."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping
from dataclasses import replace
from typing import TYPE_CHECKING, Literal

from toolang.common.errors import ToolangError
from toolang.common.time import utc_now
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
)
from toolang.lang.contracts import OutputContract
from toolang.state.state import AgentState, state_program

from ..records import ThreadPeer, SpawnContext
from ..events import ThreadCreated, RunEnd
from ..runnables import ResolvedRunnable, resolve_call_target
from ..settings import resolve_settings
from ..types import (
    ControlRef,
    RunHandle,
    RunRef,
    StepRef,
    ThreadPrefix,
    ErrorMessage,
    RunnableSettings,
)
from .common import BoundRun, Local, _RunRejected

if TYPE_CHECKING:
    from .executor import _Execution

_LOGGER = logging.getLogger(__name__)


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
) -> RunHandle:
    from .executor import _bound_runnable, _child_binding, _setup_sandbox

    dispatch_failure: RunEnd | None = None

    def admit() -> RunHandle:
        nonlocal dispatch_failure
        executor = execution.executor
        executor._require_available()
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
                bound = execution.prepare_resources(bound, runnable)
            iterations = {
                **parent.captured_iterations,
                **execution.iteration_values(step=step),
            }
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
            thread = executor.ids.issue_thread(ThreadPrefix.SPAWN.value)
            bound = replace(
                bound,
                root_run_id=bound.run_id,
                thread=thread,
                parent=None,
                call="top",
                occurrence=None,
                horizon=None,
                cwd=execution.cwd_for_run(parent.run_id),
                state_ref=ControlRef(RunRef(bound.run_id), 0),
                resource_ceiling=resources,
                captured_iterations=iterations,
            )
            output_type = runnable.output or (
                "Part[]" if isinstance(runnable, AgicDecl) else None
            )
            handle = RunHandle(bound.run_id, thread, output_type)
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
        handle, created = execution.store.accept_spawn(
            handle=handle,
            source=step,
            peer=ThreadPeer(
                type="agent", name=parent.setup.layout.name, thread=parent.thread
            ),
            resources=resources,
            limits=bound.limits,
            state=state.revision,
            runnable=_bound_runnable(bound),
            model_request=bound.model_request,
            input=bound.control_input,
            sandbox=_setup_sandbox(bound.setup),
            cwd=bound.cwd,
            created_at=bound.created_at,
            context=SpawnContext(
                bound.settings,
                bound.workspaces,
                bound.captured_iterations,
                result_contract,
            ),
        )
        if created:
            executor.notify_thread(
                ThreadCreated(
                    thread=handle.thread,
                    control=ControlRef.for_thread(handle.thread, 0),
                    origin="chat",
                    peer=ThreadPeer(
                        type="agent",
                        name=parent.setup.layout.name,
                        thread=parent.thread,
                    ),
                    created_at=bound.created_at,
                )
            )
            try:
                executor._launch(
                    bound,
                    runnable,
                    loop=asyncio.get_running_loop(),
                    tracer=None,
                    independent=True,
                )
            except Exception as exc:
                # Admission is durable even when dispatch cannot create a task.
                dispatch_failure = RunEnd(
                    run=handle.id,
                    status="failed",
                    error=ErrorMessage(str(exc)),
                    finished_at=utc_now(),
                )
                executor._persist.on_event(dispatch_failure)
        return handle

    if execution._active is None:
        handle = admit()
    else:
        async with execution._active.event_lock:
            handle = admit()
    if dispatch_failure is not None and execution.executor.root_tracer is not None:
        try:
            tracer = execution.executor.root_tracer(handle.thread)
            await tracer.on_event(dispatch_failure)
        except Exception:
            _LOGGER.exception("spawn dispatch failure observer failed")
    return handle


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
