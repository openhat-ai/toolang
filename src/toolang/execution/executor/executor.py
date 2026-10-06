"""Run acceptance, control, and recursive execution."""

from __future__ import annotations

import asyncio
from contextvars import Context
from collections.abc import Awaitable, Callable, Generator, Mapping, Sequence
from dataclasses import dataclass, field, replace
import logging
import threading
import time
from typing import Any, Literal, cast

from toolang.base.model_settings import apply_model_override
from toolang.base.types.model import Model, ModelOverride, ModelRequest
from toolang.base.types.policy import AgentCeiling, RunBindings, RunLimits
from toolang.base.types.run import ModelUsage
from toolang.base.types.message import Message, TextPart
from toolang.common.errors import ToolangError
from toolang.common.layout import (
    ensure_scratch_workspace,
)
from toolang.common.ids import IdIssuer
from toolang.common.time import utc_now
from toolang.base.utils.workspace_paths import (
    resolve_input_path,
    workspace_uri,
)
from toolang.lang.ast import (
    AgicDecl,
    FlowDecl,
    ExecStmt,
    FlowStmt,
    AwaitStmt,
    Parameter,
    Program,
    RepeatStmt,
    StructDecl,
)
from toolang.lang.contracts import (
    OutputContract,
    RunnableContract,
    validate_operation_contract,
)
from toolang.lang.input import (
    PromptInvocation,
    RunnableInput,
    CallInput,
    bind_runnable_input,
    coerce_output,
    decode_runnable_input,
    validate_runnable_arguments,
    validate_value,
)
from toolang.lang.includes import resolve_file_include
from toolang.lang.types import Array, Value, is_unnamed_ref, is_generated_ref
from toolang.plugin.models.query import first_model_ref, resolve_model
from toolang.plugin.models.resolution import (
    resolve_model_reasoning,
)
from toolang.state.state import AgentState, state_program
from toolang.state.types import StateSync
from toolang.setup import AgentSetup

from ..accounting import build_model_accounting, selected_usd_cost
from ..assembly.history import MessageHistory, adopted_horizon
from ..recall import canonical_recall
from ..calls import (
    IncludeResolver,
    materialize_model_request,
    resolve_restart_request,
    resolve_run_request,
)
from ..events import (
    RunBegin,
    RunEnd,
    RunEvent,
    RunTracer,
    StepBegin,
    StepEnd,
    ThreadEvent,
    ThreadListener,
)
from ..records import (
    CompactControlPayload,
    RecallControlPayload,
    RunControlPayload,
    LaunchContext,
    run_preparation,
    ControlRecord,
    RunRecord,
    StepRecord,
    StoredModelStepGiven,
)
from ..store import RunStore
from ..schemas import RerunRequest, RetryRequest, RunRequest
from ..types import (
    ModelAccounting,
    value_for_type,
    value_type,
    ControlTiming,
    AgentResources,
    ControlRef,
    ErrorMessage,
    ErrorRef,
    FieldRef,
    RecallTarget,
    Output,
    ControlKind,
    StepRef,
    RunRef,
    AwaitableHandle,
    ModelStepNoted,
    LoopStepNoted,
    ModelStepGiven,
    ToolStepGiven,
    ToolStepNoted,
    Occurrence,
    OccurrencePosition,
    TypedRef,
    RunCommand,
)
from ..runnables import (
    ResolvedRunnable,
    parse_runnable_ref,
    runnable_signature,
    resolve_bound_runnable,
    resolve_call_target,
    resolve_state_runnable,
)
from .steps import loop as loop_step
from .common import (
    Local,
    _MISSING,
    BoundRun,
    EventEmitter,
    _ExecutionFailed,
    _ExecuteCommitted,
    _RunRejected,
    _StepFailed,
    control_text,
    initial_locals,
    bind_inline_inputs,
    statement_has_call,
    value_parts,
    value_text,
)
from ..inspection.history import RunHistory
from ..compaction import CompactSpec, RUNNABLE as COMPACT_RUNNABLE
from ..settings import resolve_settings
from ..recall import history_variables
from .iteration import iteration_values
from .resources import (
    apply_agent_ceiling,
    resource_caps,
    resolve_agent_resources,
    resolve_path_resources,
    resolve_runnable_resources,
    snapshot_model_selection,
    validate_model_binding,
    default_workspace_workdir,
    workspace_context,
)
from .limits import (
    _RunLimitExceeded,
    _RunLimitState,
)
from ._persist import _PersistSink

_LOGGER = logging.getLogger(__name__)
_CONTROL_POLL_INTERVAL = 0.05

SetupSource = Callable[[], AgentSetup]
StateSource = Callable[[], AgentState]
StateLoad = Callable[[str], AgentState]
IncludeSource = Callable[[AgentSetup], IncludeResolver]


class _RunCanceled(asyncio.CancelledError):
    def __init__(self, control: ControlRecord) -> None:
        super().__init__(control_text(control) or "canceled")
        self.control = control


@dataclass(slots=True)
class _ActiveRun:
    task: asyncio.Task[RunRecord]
    tracer: RunTracer | None
    root_run_id: str
    loop: asyncio.AbstractEventLoop = field(repr=False)
    interruption: ControlRecord | None = None
    controls: dict[str, dict[int, ControlRecord]] = field(
        default_factory=dict,
        repr=False,
    )
    event_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    ended: set[str] = field(default_factory=set, repr=False)
    execution: _Execution | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class RunSpec:
    """Immutable inputs required to execute one runnable."""

    setup: AgentSetup
    state: AgentState
    thread: str
    bindings: RunBindings
    limits: RunLimits
    model_request: ModelRequest | None = None
    workdir: str | None = None
    workdir_base: str | None = None
    ceilings: tuple[AgentCeiling, ...] = ()
    input: RunnableInput = field(default_factory=CallInput)
    authored_input: CallInput[str] | None = None
    authored_commands: tuple[RunCommand, ...] = ()
    authored_session_commands: tuple[RunCommand, ...] = ()
    prompt_invocations: tuple[PromptInvocation, ...] = ()
    horizon: RunRef | StepRef | None = None
    all_tools: bool = False
    launch_context: LaunchContext | None = None
    resource_ceiling: AgentResources | None = None


@dataclass(frozen=True, slots=True)
class LocalRunHandle(Awaitable[RunRecord]):
    """One locally accepted run that can be controlled and awaited."""

    run_id: str
    executor: RunExecutor = field(repr=False)
    task: asyncio.Task[RunRecord] = field(repr=False)

    def cancel(
        self,
        *,
        timing: ControlTiming = "immediate",
        request_id: str | None = None,
        reason: str | None = None,
    ) -> ControlRecord:
        """Persist a cancel control for this run."""

        return self.executor.cancel(
            run_id=self.run_id,
            timing=timing,
            request_id=request_id,
            reason=reason,
        )

    def steer(
        self,
        message: Message,
        *,
        timing: ControlTiming = "next_step",
        request_id: str | None = None,
    ) -> ControlRecord:
        """Persist a steer control for this run."""

        return self.executor.steer(
            run_id=self.run_id,
            message=message,
            timing=timing,
            request_id=request_id,
        )

    def cancel_control(self, index: int) -> ControlRecord:
        """Revoke one pending steer or cancel control for this run."""

        return self.executor.cancel_control(run_id=self.run_id, index=index)

    def __await__(self) -> Generator[Any, None, RunRecord]:
        return self._wait().__await__()

    async def _wait(self) -> RunRecord:
        try:
            return await asyncio.shield(self.task)
        except asyncio.CancelledError:
            if self.task.cancelled():
                record = self.executor.store.get_run(run_id=self.run_id)
                if record is not None and record.status not in {"pending", "running"}:
                    return record
            raise


class RunExecutor:
    """Accept, control, and execute runs against durable execution truth."""

    def __init__(
        self,
        store: RunStore,
        ids: IdIssuer,
        *,
        setup: SetupSource | None = None,
        state: StateSource | None = None,
        load_state: StateLoad | None = None,
        sync_state: StateSync | None = None,
        include: IncludeSource | None = None,
        default_workdir: str | None = None,
    ) -> None:
        if (setup is None) != (state is None) or (setup is None) != (
            load_state is None
        ):
            raise TypeError(
                "run request execution requires setup, state, and load_state together"
            )
        self.store = store
        self.ids = ids
        self._setup = setup
        self._state = state
        self._load_state = load_state
        self._sync_state = sync_state
        self._include = include
        self._default_workdir_override = default_workdir
        self._persist = _PersistSink(self.store)
        self._control_poll_interval = _CONTROL_POLL_INTERVAL
        self._active: dict[str, _ActiveRun] = {}
        self._tasks: dict[asyncio.Task[RunRecord], tuple[str, _ActiveRun]] = {}
        self._active_lock = threading.Lock()
        self._monitor_task: asyncio.Task[None] | None = None
        self._control_revision = self.store.latest_run_control_revision()
        self._stopped = False
        # Host observation is separate from the foreground caller's tracer.
        self.root_tracer: Callable[[str], RunTracer] | None = None
        self.thread_listener: ThreadListener | None = None

    def notify_thread(self, event: ThreadEvent) -> None:
        """Notify the host of a committed thread created during execution."""

        if self.thread_listener is not None:
            try:
                self.thread_listener.on_event(event)
            except Exception:
                _LOGGER.exception("thread listener event handling failed")

    def start(self) -> None:
        """Start this executor lifecycle."""

        self._require_available()

    def _default_workdir(self, setup: AgentSetup, state: AgentState) -> str:
        environment = setup.environment
        if environment is None or environment.workspace_location == "host":
            ensure_scratch_workspace(setup.layout.home)
        return self.default_workdir(setup, state)

    def default_workdir(self, setup: AgentSetup, state: AgentState) -> str:
        """Inspect the runtime default without preparing directories or a Run."""
        return default_workspace_workdir(
            setup, state, workdir=self._default_workdir_override
        )

    def _valid_workdir(
        self, setup: AgentSetup, state: AgentState, value: str
    ) -> str | None:
        try:
            path, name, relative = resolve_input_path(
                value,
                workspace_context(setup, state, self._default_workdir(setup, state)),
            )
        except (OSError, ToolangError, ValueError):
            return None
        if not path.is_dir():
            return None
        return workspace_uri(name, relative)

    def initial_workdir(
        self,
        setup: AgentSetup,
        state: AgentState,
        thread: str | None = None,
    ) -> str:
        """Resolve the latest finished root workdir or runtime default."""
        default = self._default_workdir(setup, state)
        runs = (
            self.store.list_runs(thread_id=thread, limit=None)
            if thread is not None
            else self.store.list_runs(limit=None)
        )
        for run in runs:
            if run.parent is not None or run.finished_at is None:
                continue
            try:
                current = self.store.current_cwd(run.id)
            except (KeyError, ValueError):
                return default
            return (
                (self._valid_workdir(setup, state, current) or default)
                if current
                else default
            )
        return default

    def resolve_workdir(
        self,
        setup: AgentSetup,
        state: AgentState,
        *,
        workdir: str | None,
        workdir_base: str | None = None,
        thread: str | None = None,
        inherit_thread_workdir: bool = True,
    ) -> str:
        """Resolve a requested workdir to its canonical workspace location."""
        previous = (
            self.initial_workdir(setup, state, thread)
            if inherit_thread_workdir
            else self._default_workdir(setup, state)
        )
        if workdir is None:
            return previous
        base = workdir_base or previous
        context = workspace_context(setup, state, base)
        target, name, relative = resolve_input_path(workdir, context)
        if not target.is_dir():
            raise ToolangError(f"workdir target is not a directory: {workdir}")
        return workspace_uri(name, relative)

    def _initial_workdir(
        self, spec: RunSpec, *, inherit_thread_workdir: bool = True
    ) -> str:
        """Resolve one root Run's workdir from its explicit path or thread history."""
        return self.resolve_workdir(
            spec.setup,
            spec.state,
            workdir=spec.workdir,
            workdir_base=spec.workdir_base,
            thread=spec.thread,
            inherit_thread_workdir=inherit_thread_workdir,
        )

    def run(
        self,
        spec: RunSpec | RunRequest,
        *,
        run_id: str | None = None,
        request_id: str | None = None,
        tracer: RunTracer | None = None,
    ) -> LocalRunHandle:
        """Accept one top-level run and immediately return its local handle."""

        self._require_available()
        if isinstance(spec, RunRequest):
            if run_id is not None or request_id is not None:
                raise ValueError(
                    "resolved run identity cannot override a caller run request"
                )
            setup, state = self._current_snapshots()
            request_id = spec.request_id
            spec = resolve_run_request(
                spec,
                setup=setup,
                state=state,
                include=self._include_resolver(setup),
            )
        loop = asyncio.get_running_loop()
        if spec.horizon is None:
            spec = replace(
                spec,
                horizon=(
                    output.ref
                    if (output := RunHistory(self.store).get_compaction(spec.thread))
                    else None
                ),
            )
        spec = replace(spec, workdir=self._initial_workdir(spec), workdir_base=None)
        sandbox = _setup_sandbox(spec.setup)
        runnable, input, agent_resources, resources = _prepare_run_spec(spec)
        if not isinstance(spec.limits, RunLimits):
            raise TypeError("run limits must be RunLimits")
        bound = _bind_run(
            spec,
            runnable=runnable,
            run_id=run_id or self.ids.issue_run(),
            input=input,
            agent_resources=agent_resources,
            resources=resources,
        )
        self.store.accept_run(
            run_id=bound.run_id,
            parent=None,
            thread=bound.thread,
            resources=resources,
            limits=bound.limits,
            state=bound.state.revision,
            runnable=_bound_runnable(bound),
            model_request=bound.model_request,
            input=bound.control_input,
            sandbox=sandbox,
            cwd=bound.cwd,
            occurrence=bound.occurrence,
            request_id=request_id,
            created_at=bound.created_at,
            authored_input=spec.authored_input,
            authored_commands=spec.authored_commands,
            authored_session_commands=spec.authored_session_commands,
            prompt_invocations=spec.prompt_invocations,
            launch_context=spec.launch_context,
            horizon=bound.horizon,
        )
        return self._launch(bound, runnable, loop=loop, tracer=tracer)

    def rerun(
        self,
        source: str | RerunRequest,
        *,
        setup: AgentSetup | None = None,
        state: AgentState | None = None,
        ceiling: AgentCeiling = AgentCeiling(),
        model: str | None = None,
        model_request: ModelRequest | None = None,
        limits: RunLimits | None = None,
        run_id: str | None = None,
        request_id: str | None = None,
        tracer: RunTracer | None = None,
    ) -> LocalRunHandle:
        """Start a new root run from one visible source run's invocation."""

        self._require_available()
        if isinstance(source, RerunRequest):
            if (
                setup is not None
                or state is not None
                or model_request is not None
                or request_id is not None
            ):
                raise ValueError(
                    "resolved rerun inputs cannot override a caller rerun request"
                )
            request = source
            setup, state = self._current_snapshots()
            resolved = resolve_restart_request(request, setup=setup, state=state)
            source = request.source
            setup = resolved.setup
            state = resolved.state
            ceiling = resolved.ceiling
            model_request = resolved.model
            model = model_request.ref if model_request is not None else None
            model_override = resolved.model_override
            limits = resolved.limits
            request_id = request.request_id
        else:
            model_override = None
        if setup is None or state is None:
            raise TypeError("rerun requires resolved setup and state")
        original = self.store.get_run(run_id=source)
        if original is None or original.parent is not None:
            raise ValueError(f"source root run not found: {source}")
        if original.status not in {"succeeded", "failed", "canceled"}:
            raise ValueError(f"rerun source is not terminal: {source}")
        loop = asyncio.get_running_loop()
        sandbox = _setup_sandbox(setup)
        spec = self._source_spec(
            source,
            setup=setup,
            state=state,
            ceiling=ceiling,
            model=model,
            model_request=model_request,
            model_override=model_override,
            limits=limits if limits is not None else setup.limits,
        )
        spec = replace(
            spec,
            horizon=(
                output.ref
                if (output := RunHistory(self.store).get_compaction(spec.thread))
                else None
            )
            or self.store.run_horizon(source),
        )
        spec = replace(
            spec,
            workdir=self._initial_workdir(spec, inherit_thread_workdir=False),
            workdir_base=None,
        )
        runnable, input, agent_resources, resources = _prepare_run_spec(spec)
        bound = _bind_run(
            spec,
            runnable=runnable,
            run_id=run_id or self.ids.issue_run(),
            input=input,
            agent_resources=agent_resources,
            resources=resources,
        )
        self.store.accept_run(
            run_id=bound.run_id,
            parent=None,
            thread=bound.thread,
            resources=resources,
            limits=bound.limits,
            state=bound.state.revision,
            runnable=_bound_runnable(bound),
            model_request=bound.model_request,
            input=bound.control_input,
            sandbox=sandbox,
            cwd=bound.cwd,
            occurrence=bound.occurrence,
            request_id=request_id,
            created_at=bound.created_at,
            authored_input=spec.authored_input,
            authored_commands=spec.authored_commands,
            authored_session_commands=spec.authored_session_commands,
            prompt_invocations=spec.prompt_invocations,
            launch_context=spec.launch_context,
            horizon=bound.horizon,
        )
        return self._launch(bound, runnable, loop=loop, tracer=tracer)

    def retry(
        self,
        run_id: str | RetryRequest,
        *,
        setup: AgentSetup | None = None,
        state: AgentState | None = None,
        anchor: StepRef | str | None = None,
        ceiling: AgentCeiling = AgentCeiling(),
        limits: RunLimits | None = None,
        request_id: str | None = None,
        tracer: RunTracer | None = None,
    ) -> LocalRunHandle:
        """Reopen one terminal root run from a durable step boundary."""

        self._require_available()
        model_request: ModelRequest | None = None
        if isinstance(run_id, RetryRequest):
            if setup is not None or state is not None or request_id is not None:
                raise ValueError(
                    "resolved retry inputs cannot override a caller retry request"
                )
            request = run_id
            setup = self._current_setup()
            state = self._recorded_state(request.source)
            resolved = resolve_restart_request(request, setup=setup, state=state)
            run_id = request.source
            setup = resolved.setup
            state = resolved.state
            anchor = request.anchor
            ceiling = resolved.ceiling
            model_request = resolved.model
            limits = resolved.limits
            request_id = request.request_id
        if setup is None or state is None:
            raise TypeError("retry requires resolved setup and state")
        loop = asyncio.get_running_loop()
        sandbox = _setup_sandbox(setup)
        self._require_retry_compatible(run_id, state, sandbox=sandbox)
        spec = self._source_spec(
            run_id,
            setup=setup,
            state=state,
            ceiling=ceiling,
            model=None,
            model_request=model_request,
            limits=limits if limits is not None else setup.limits,
        )
        runnable, input, agent_resources, resources = _prepare_run_spec(spec)
        bound = _bind_run(
            spec,
            runnable=runnable,
            run_id=run_id,
            input=input,
            agent_resources=agent_resources,
            resources=resources,
        )
        _reopened, control, _trimmed = self.store.accept_retry(
            run_id=run_id,
            anchor=StepRef.parse(anchor) if anchor is not None else None,
            resources=resources,
            limits=bound.limits,
            state=bound.state.revision,
            model_request=bound.model_request,
            sandbox=sandbox,
            request_id=request_id,
            created_at=bound.created_at,
        )
        bound = replace(
            bound, control_index=control.index, horizon=self.store.run_horizon(run_id)
        )
        return self._launch(
            bound,
            runnable,
            loop=loop,
            tracer=tracer,
            retry=control,
        )

    def _current_setup(self) -> AgentSetup:
        source = self._setup
        if source is None:
            raise RuntimeError("run executor has no request snapshot sources")
        return source()

    def _current_snapshots(self) -> tuple[AgentSetup, AgentState]:
        source = self._state
        if source is None:
            raise RuntimeError("run executor has no request snapshot sources")
        return self._current_setup(), source()

    def _include_resolver(self, setup: AgentSetup) -> IncludeResolver:
        if self._include is not None:
            return self._include(setup)
        base = (
            setup.environment.working_directory
            if setup.environment is not None
            else setup.layout.home
        )
        return lambda reference: resolve_file_include(reference, base=base)

    def _recorded_state(self, run_id: str) -> AgentState:
        load = self._load_state
        if load is None:
            raise RuntimeError("run executor has no request snapshot sources")
        run = self.store.get_run(run_id=run_id)
        if run is None or run.parent is not None:
            raise ValueError(f"root run not found: {run_id}")
        revision = self.store.resolve_state_revision(run.state)
        try:
            state = load(revision)
        except (KeyError, OSError, RuntimeError, TypeError, ValueError) as exc:
            raise ValueError(
                f"retry state snapshot is not available: {revision}"
            ) from exc
        if not isinstance(state, AgentState) or state.revision != revision:
            raise ValueError(f"retry state snapshot is not available: {revision}")
        return state

    def _source_spec(
        self,
        run_id: str,
        *,
        setup: AgentSetup,
        state: AgentState,
        ceiling: AgentCeiling,
        model: str | None,
        model_request: ModelRequest | None = None,
        model_override: ModelOverride | None = None,
        limits: RunLimits,
    ) -> RunSpec:
        run = self.store.get_run(run_id=run_id)
        if run is None or run.parent is not None:
            raise ValueError(f"root run not found: {run_id}")
        preparation = run_preparation(run, self.store.list_run_controls(run_id=run_id))
        entry = self.store.get_run_control(run_id=run_id, index=0)
        entry_payload = entry.payload if entry is not None else None
        captured = (
            entry_payload.launch_context
            if isinstance(entry_payload, RunControlPayload)
            else None
        )
        module, _, ref = (
            preparation.runnable.rpartition("::")
            if "::" in preparation.runnable
            else ("", "", preparation.runnable)
        )
        runnable, kind = parse_runnable_ref(ref)
        resolved_module, declaration = resolve_state_runnable(
            state, runnable, kind=kind
        )
        if module and resolved_module != module:
            raise ValueError(f"run runnable module changed: {preparation.runnable}")
        persisted_model_request = preparation.model_request
        selected_model_request = model_request or (
            ModelRequest(model) if model is not None else persisted_model_request
        )
        default_model_request = setup.defaults.model
        if default_model_request is None:
            fallback = first_model_ref(setup.models_effective())
            default_model_request = (
                ModelRequest(fallback) if fallback is not None else None
            )
        selected_model_request = apply_model_override(
            selected_model_request,
            default_model_request,
            model_override,
        )
        if selected_model_request is not None:
            selected_model_request = materialize_model_request(
                selected_model_request,
                setup=setup,
            )
        return RunSpec(
            setup=setup,
            state=state,
            thread=str(run.thread),
            bindings=RunBindings(
                runnable=f"{declaration.kind}:{runnable}",
                model=(
                    selected_model_request.ref
                    if selected_model_request is not None
                    else None
                ),
            ),
            model_request=selected_model_request,
            limits=limits,
            ceilings=(
                (ceiling,)
                if any(
                    value is not None
                    for value in (
                        ceiling.models,
                        ceiling.tools,
                        ceiling.psyches,
                        ceiling.skills,
                        ceiling.services,
                        ceiling.prompts,
                    )
                )
                else ()
            ),
            input=_resolve_stored_input(
                self.store,
                preparation.input,
            ),
            authored_input=preparation.authored_input,
            authored_commands=preparation.authored_commands,
            authored_session_commands=preparation.authored_session_commands,
            prompt_invocations=preparation.prompt_invocations,
            launch_context=captured,
            resource_ceiling=entry_payload.resources
            if captured is not None and isinstance(entry_payload, RunControlPayload)
            else None,
            workdir=preparation.cwd if captured is not None else None,
        )

    def _require_retry_compatible(
        self, run_id: str, state: AgentState, *, sandbox: str
    ) -> None:
        """Reject retry before mutation when its execution snapshot changed."""

        run = self.store.get_run(run_id=run_id)
        if run is None or run.parent is not None:
            raise ValueError(f"root run not found: {run_id}")
        control = self.store.get_run_control(
            run_id=str(run.control.target),
            index=0,
        )
        if control is None or not isinstance(control.payload, RunControlPayload):
            raise ValueError(f"run preparation not found: {run_id}")
        if self.store.resolve_state_revision(run.state) != state.revision:
            raise ValueError(
                f"retry state no longer matches original run: {run_id}; use rerun"
            )
        if control.payload.sandbox is None:
            raise ValueError(f"retry sandbox is unknown for run {run_id}; use rerun")
        if control.payload.sandbox != sandbox:
            raise ValueError(
                f"retry sandbox {sandbox} does not match original sandbox "
                f"{control.payload.sandbox} for run {run_id}; use rerun"
            )
        if state.workspaces:
            if self._state is None:
                raise ValueError("retry requires current workspace authorization")
            workspaces = self._state().workspaces
            for name, path in state.workspaces.items():
                if workspaces.get(name) != path:
                    raise ValueError(
                        f"retry workspace {name!r} is no longer authorized "
                        "at its recorded path; use rerun"
                    )

    def _launch(
        self,
        bound: BoundRun,
        runnable: AgicDecl | FlowDecl,
        *,
        loop: asyncio.AbstractEventLoop,
        tracer: RunTracer | None,
        retry: ControlRecord | None = None,
        independent: bool = False,
    ) -> LocalRunHandle:
        if tracer is None and self.root_tracer is not None:
            tracer = self.root_tracer(bound.thread)
        task = asyncio.create_task(
            self._execute_owned(bound, runnable, tracer=tracer, retry=retry),
            name=f"toolang-run-{bound.run_id}",
            context=Context() if independent else None,
        )
        active = _ActiveRun(
            task=task,
            tracer=tracer,
            root_run_id=bound.root_run_id,
            loop=loop,
        )
        with self._active_lock:
            self._active[bound.run_id] = active
        self._tasks[task] = (bound.run_id, active)
        task.add_done_callback(self._task_done)
        try:
            self._ensure_monitor(bound.setup.layout.name)
        except Exception:
            task.cancel()
            self._tasks.pop(task, None)
            with self._active_lock:
                self._active.pop(bound.run_id, None)
            raise
        return LocalRunHandle(bound.run_id, self, task)

    def validate(self, spec: RunSpec) -> None:
        """Validate one immutable run spec without accepting a run."""

        _prepare_run_spec(spec)

    async def _execute_owned(
        self,
        bound: BoundRun,
        runnable: AgicDecl | FlowDecl,
        *,
        tracer: RunTracer | None,
        retry: ControlRecord | None = None,
    ) -> RunRecord:
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("run execution requires an asyncio task")
        with self._active_lock:
            active = self._active.get(bound.run_id)
        if active is None or active.task is not task or active.tracer is not tracer:
            raise RuntimeError(f"run ownership missing: {bound.run_id}")
        started_at = time.perf_counter()
        emit = self._handler(active)
        execution = _Execution(
            self,
            root=bound,
            active=active,
            retry=retry,
        )
        with self._active_lock:
            active.execution = execution
        timeout = execution.schedule_time_limit(task)
        try:
            await execution.execute(
                bound,
                runnable,
            )
        except asyncio.CancelledError:
            await self._ensure_terminal(bound.run_id, emit=emit, status="canceled")
        except Exception as exc:
            await self._ensure_terminal(
                bound.run_id,
                emit=emit,
                status="failed",
                error=ErrorMessage(str(exc) or type(exc).__name__),
            )
        finally:
            if timeout is not None:
                timeout.cancel()
            with self._active_lock:
                for run_id in tuple(self._active):
                    if self._active.get(run_id) is active:
                        self._active.pop(run_id, None)
        result = self.store.get_run(run_id=bound.run_id)
        if result is None:
            raise RuntimeError(f"run projection missing: {bound.run_id}")
        _LOGGER.info(
            "Run finished thread=%s run=%s status=%s duration_ms=%s",
            result.thread,
            result.id,
            result.status,
            max(0, round((time.perf_counter() - started_at) * 1000)),
        )
        return result

    def cancel(
        self,
        *,
        run_id: str,
        timing: ControlTiming = "immediate",
        request_id: str | None = None,
        reason: str | None = None,
    ) -> ControlRecord:
        """Persist one cancel control for the process that owns the run."""

        self._require_available()
        control = self.store.accept_run_control(
            run_id=run_id,
            kind="cancel",
            timing=timing,
            input=CallInput({"_": reason} if reason is not None else {}),
            request_id=request_id,
            created_at=utc_now(),
        )
        self._observe_control(control)
        return control

    def steer(
        self,
        *,
        run_id: str,
        message: Message,
        timing: ControlTiming,
        request_id: str | None = None,
    ) -> ControlRecord:
        """Persist one steer control for the process that owns the run."""

        self._require_available()
        if message.role != "user":
            raise ValueError("run steer requires a user message")
        _ = message.parts
        control = self.store.accept_run_control(
            run_id=run_id,
            kind="steer",
            timing=timing,
            input=CallInput({"_": Array("Part[]", tuple(message.parts))}),
            request_id=request_id,
            created_at=utc_now(),
        )
        self._observe_control(control)
        return control

    def cancel_control(self, *, run_id: str, index: int) -> ControlRecord:
        """Revoke one pending steer or cancel control."""

        self._require_available()
        control = self.store.cancel_run_control(
            run_id=run_id,
            index=index,
            canceled_at=utc_now(),
        )
        self._observe_control(control)
        return control

    async def stop(self) -> None:
        """Cancel and await all runs owned by this executor."""

        if self._stopped:
            return
        self._stopped = True
        owned = tuple(self._tasks.items())
        for task, _run in owned:
            if not task.done():
                task.cancel()
        if owned:
            await asyncio.gather(
                *(task for task, _run in owned),
                return_exceptions=True,
            )
        await asyncio.sleep(0)
        for _task, (run_id, active) in owned:
            await self._ensure_terminal(
                run_id,
                emit=self._handler(active),
                status="canceled",
            )
        with self._active_lock:
            owned_tasks = {task for task, _run in owned}
            for run_id, active in tuple(self._active.items()):
                if active.task in owned_tasks:
                    self._active.pop(run_id, None)
        monitor = self._monitor_task
        self._monitor_task = None
        if monitor is not None and not monitor.done():
            monitor.cancel()
            await asyncio.gather(monitor, return_exceptions=True)

    def _require_available(self) -> None:
        if self._stopped:
            raise RuntimeError("run executor is stopped")

    def _task_done(self, task: asyncio.Task[RunRecord]) -> None:
        owned = self._tasks.pop(task, None)
        if owned is None:
            return
        run_id, active = owned
        if not task.cancelled() and (error := task.exception()) is not None:
            _LOGGER.error(
                "Run task failed outside runtime handling run=%s error=%r",
                run_id,
                str(error) or type(error).__name__,
            )
        with self._active_lock:
            for active_run_id, candidate in tuple(self._active.items()):
                if candidate is active:
                    self._active.pop(active_run_id, None)

    def _handler(self, active: _ActiveRun) -> EventEmitter:
        async def emit(event: RunEvent) -> None:
            await self._emit_event(active, event)

        return emit

    async def _emit_event(self, active: _ActiveRun, event: RunEvent) -> None:
        interruption: asyncio.CancelledError | None = None
        while True:
            try:
                await active.event_lock.acquire()
                break
            except asyncio.CancelledError as exc:
                if not isinstance(event, StepEnd):
                    raise
                step = self.store.get_step(ref=event.step)
                if step is None or step.status == "running":
                    raise
                # Admission may have committed the Step before event delivery.
                # Finish delivering that fact before propagating cancellation.
                interruption = exc
        try:
            await self._emit_event_locked(active, event)
        finally:
            active.event_lock.release()
        if interruption is not None:
            raise interruption

    async def _emit_event_locked(
        self,
        active: _ActiveRun,
        event: RunEvent,
    ) -> None:
        event_run = _run_event_id(event)
        if event_run in active.ended:
            return
        if (
            isinstance(event, StepEnd)
            and event.status == "canceled"
            and active.interruption is not None
        ):
            event = replace(event, aborted_by=active.interruption.ref)
        with self.store.write_transaction():
            event = self._persist.on_event(event)
            self._update_control_state(event)
        if isinstance(event, StepBegin) and active.execution is not None:
            active.execution._adopt_step_relations(event)
        if (
            isinstance(event, StepEnd)
            and event.kind == "tool"
            and event.status == "succeeded"
            and active.execution is not None
        ):
            stored = self.store.get_step(ref=event.step)
            if (
                stored is not None
                and isinstance(stored.given, ToolStepGiven)
                and stored.given.call.name == "_toolang__chdir"
            ):
                active.execution._cwd_cache[event.step.run_id] = self.store.current_cwd(
                    event.step.run_id
                )
        self._update_cached_control_state(event)
        self._track_active_run(event, active)
        if isinstance(event, RunEnd):
            active.ended.add(event.run)
            if active.execution is not None:
                active.execution._active_bindings.pop(event.run, None)
                active.execution._cwd_cache.pop(event.run, None)
        if active.tracer is not None:
            try:
                await active.tracer.on_event(event)
            except Exception:
                _LOGGER.exception("run tracer event handling failed")

    def _update_control_state(self, event: RunEvent) -> None:
        if isinstance(event, StepBegin):
            for ref in event.preceded_by:
                self.store.finish_run_controls(
                    run_id=str(ref.target),
                    indexes=(ref.index,),
                    finished_at=event.started_at,
                )
            return
        if isinstance(event, StepEnd) and event.aborted_by is not None:
            control = self.store.get_run_control(
                run_id=str(event.aborted_by.target), index=event.aborted_by.index
            )
            # An immediate steer is adopted by the next model begin, not by the
            # interrupted end. A cancel has no subsequent input consumer.
            if control is not None and control.kind == "cancel":
                self.store.finish_run_controls(
                    run_id=str(control.target),
                    indexes=(control.index,),
                    finished_at=event.finished_at,
                )
            return
        if isinstance(event, RunEnd):
            if event.control is not None:
                self.store.finish_run_controls(
                    run_id=str(event.control.target),
                    indexes=(event.control.index,),
                    finished_at=event.finished_at,
                )
            self.store.fail_pending_run_controls(
                run_id=event.run,
                finished_at=event.finished_at,
                error="run ended before the control could be applied",
            )

    def _track_active_run(self, event: RunEvent, active: _ActiveRun) -> None:
        if isinstance(event, RunBegin):
            with self._active_lock:
                self._active[event.run] = active
        elif isinstance(event, RunEnd):
            with self._active_lock:
                if self._active.get(event.run) is active:
                    self._active.pop(event.run, None)

    def _register_child_run(self, *, run_id: str, root_run_id: str) -> None:
        with self._active_lock:
            active = self._active.get(root_run_id)
            if active is None:
                raise RuntimeError(f"root run ownership missing: {root_run_id}")
            self._active[run_id] = active

    def _observe_control(self, control: ControlRecord) -> None:
        if control.kind == "run":
            return
        cancel: asyncio.Task[RunRecord] | None = None
        loop: asyncio.AbstractEventLoop | None = None
        with self._active_lock:
            active = self._active.get(str(control.target))
            if active is None:
                return
            controls = active.controls.setdefault(str(control.target), {})
            if control.status == "pending":
                observed = control.index in controls
                controls[control.index] = control
                if (
                    not observed
                    and control.kind in {"steer", "cancel"}
                    and control.timing == "immediate"
                ):
                    if control.kind == "cancel" and active.execution is not None:
                        target_task = active.execution._background_tasks.get(
                            str(control.target)
                        )
                        if target_task is not None:

                            def cancel_target() -> None:
                                assert target_task is not None
                                self.store.claim_run_controls(
                                    run_id=str(control.target), indexes=(control.index,)
                                )
                                target_task.cancel()

                            active.loop.call_soon_threadsafe(cancel_target)
                            return
                    cancel = active.task
                    loop = active.loop
            else:
                controls.pop(control.index, None)
                if not controls:
                    active.controls.pop(str(control.target), None)
        if cancel is not None and loop is not None and not cancel.done():

            def interrupt() -> None:
                if cancel.done():
                    return
                previous = active.interruption
                if (
                    previous is not None
                    and previous.kind == "cancel"
                    and str(previous.target)
                    in self.store.run_ancestry(run_id=str(control.target))
                ):
                    # A descendant control cannot supersede cancellation of
                    # its enclosing Run or interrupt that Run's cleanup again.
                    return
                if control.kind == "cancel":
                    claimed = self.store.claim_run_controls(
                        run_id=str(control.target), indexes=(control.index,)
                    )
                    if control.index not in claimed:
                        return
                else:
                    current = self.store.get_run_control(
                        run_id=str(control.target), index=control.index
                    )
                    if current is None or current.status != "pending":
                        return
                active.interruption = control
                cancel.cancel()

            loop.call_soon_threadsafe(interrupt)

    def _pending_controls(
        self,
        *,
        run_id: str,
        kind: ControlKind,
    ) -> tuple[ControlRecord, ...]:
        self._refresh_controls()
        with self._active_lock:
            active = self._active.get(run_id)
            if active is None:
                return ()
            controls = active.controls.get(run_id, {})
            return tuple(
                control
                for _index, control in sorted(controls.items())
                if control.kind == kind and control.status == "pending"
            )

    def _claim_controls(
        self,
        *,
        run_id: str,
        controls: Sequence[ControlRecord],
    ) -> tuple[ControlRecord, ...]:
        if not controls:
            return ()
        claimed = self.store.claim_run_controls(
            run_id=run_id,
            indexes=tuple(control.index for control in controls),
        )
        return tuple(control for control in controls if control.index in claimed)

    def _update_cached_control_state(self, event: RunEvent) -> None:
        if isinstance(event, RunBegin):
            return
        if isinstance(event, StepBegin):
            run_id = event.step.run_id
            indexes = {
                ref.index for ref in event.preceded_by if str(ref.target) == run_id
            }
        elif isinstance(event, RunEnd):
            run_id = event.run
            indexes = None
        else:
            return
        with self._active_lock:
            active = self._active.get(run_id)
            if active is None:
                return
            if indexes is None:
                active.controls.pop(run_id, None)
                return
            controls = active.controls.get(run_id)
            if controls is None:
                return
            for index in indexes:
                controls.pop(index, None)
            if not controls:
                active.controls.pop(run_id, None)

    def _refresh_controls(self) -> bool:
        revision, controls = self.store.changed_run_controls(
            after_revision=self._control_revision
        )
        if not controls:
            return False
        for control in controls:
            self._observe_control(control)
        self._control_revision = revision
        return True

    def _ensure_monitor(self, agent_name: str) -> None:
        if self._monitor_task is None or self._monitor_task.done():
            self._monitor_task = asyncio.create_task(
                self._monitor_controls(), name=f"toolang-controls-{agent_name}"
            )

    async def _monitor_controls(self) -> None:
        while True:
            await asyncio.sleep(self._control_poll_interval)
            self._refresh_controls()

    async def _ensure_terminal(
        self,
        run_id: str,
        *,
        emit: EventEmitter,
        status: Literal["failed", "canceled"],
        error: ErrorMessage | ErrorRef | None = None,
    ) -> None:
        record = self.store.get_run(run_id=run_id)
        if record is not None and record.status not in {"pending", "running"}:
            return
        cancellation = next(
            iter(self.store.pending_run_controls(run_id=run_id, kind="cancel")),
            None,
        )
        with self._active_lock:
            active = self._active.get(run_id)
            interruption = active.interruption if active is not None else None
        if (
            interruption is not None
            and interruption.kind == "cancel"
            and interruption.target == RunRef(run_id)
        ):
            # RunBegin delivery may already have applied the target's cancel.
            cancellation = interruption
        await emit(
            RunEnd(
                run=run_id,
                status=status,
                control=(
                    ControlRef(RunRef(run_id), cancellation.index)
                    if cancellation is not None
                    else None
                ),
                error=error or ErrorMessage(control_text(cancellation) or status),
                finished_at=utc_now(),
            )
        )


class _Execution:
    """Private state for one root run and its recursive child-run tree."""

    def __init__(
        self,
        executor: RunExecutor,
        *,
        root: BoundRun,
        active: _ActiveRun | None = None,
        emit: EventEmitter | None = None,
        retry: ControlRecord | None = None,
    ) -> None:
        if (active is None) == (emit is None):
            raise TypeError(
                "execution requires exactly one active run or event emitter"
            )
        self.executor = executor
        self.setup = root.setup
        self.layout = root.setup.layout
        if root.agent_resources is None:
            raise RuntimeError(f"agent resources missing: {root.run_id}")
        self._agent_resources = root.agent_resources
        self.date = root.created_at.partition("T")[0]
        self.timezone = "UTC"
        self._active = active
        self._emit_trace = emit
        self._step_states: dict[StepRef, tuple[AgentState, ControlRef]] = {}
        self._preceding_controls: list[ControlRef] = []
        self._limits = _RunLimitState(root.limits)
        self._retry = retry
        if retry is not None:
            self._restore_model_limits(root.run_id)
        self._run_outputs: dict[str, Output] = {}
        self._background_tasks: dict[str, asyncio.Task[None]] = {}
        self._background_owners: dict[str, str] = {}
        self._background_horizons: dict[str, RunRef | StepRef | None] = {}
        self._active_bindings: dict[str, BoundRun] = {root.run_id: root}
        # Durable controls are authoritative; this is only an online projection.
        self._cwd_cache: dict[str, str] = {}
        self.repeat_progress: dict[StepRef, loop_step.LoopProgress] = {}
        self._history: MessageHistory | None = None
        self._history_horizon = root.horizon
        self._history_versions = {root.horizon}
        self._history_root = root.root_run_id
        self._step_horizons: dict[StepRef, RunRef | StepRef | None] = {}
        self._runtime_controls: dict[str, dict[int, ControlRecord]] = {}
        self._runtime_cursors: dict[str, int] = {}
        self._handle_views: dict[StepRef, dict[str, dict[str, str]]] = {}

    def message_history(self) -> MessageHistory:
        if self._history is None:
            root = next(iter(self._active_bindings.values()))
            self._history = self.store.message_history(root.root_run_id)
        return self._history

    def thread_model_ref(self) -> str | None:
        """Use the current root binding as the thread's compaction reference."""
        root = self._active_bindings[self._history_root]
        return (
            root.model_request.ref
            if root.model_request is not None
            else root.bindings.model
        )

    def cwd_for_run(self, run_id: str) -> str:
        """Read the Run's committed location, caching only while it is active."""
        if run_id not in self._cwd_cache:
            self._cwd_cache[run_id] = self.store.current_cwd(run_id)
        return self._cwd_cache[run_id]

    def state_snapshot(self, run_id: str) -> tuple[AgentState, ControlRef]:
        """Read an accepted Run's immutable binding."""
        binding = self._active_bindings[run_id]
        return binding.state, binding.state_ref

    def latest_state(self) -> AgentState:
        """Capture the latest fully published State without preparing source."""
        if self.executor._state is None:
            raise ToolangError("Published Agent State is unavailable in this executor")
        return self.executor._state()

    def compact(
        self, step: StepRef, horizon: RunRef | StepRef
    ) -> tuple[ControlRef, ...]:
        """Record the result for adoption, retaining its online receipt facts."""
        pending = self.runtime_controls(step.run_id)
        if self.horizon_for(step.run_id) == horizon:
            return ()
        for control in reversed(pending):
            if isinstance(control.payload, CompactControlPayload):
                if control.payload.horizon == horizon:
                    return (control.ref,)
                break
        with self.store.write_transaction():
            self.store.publish_compaction(horizon, roots=self.message_history().roots)
            control = self.store.accept_compact_control(
                run_id=step.run_id,
                horizon=horizon,
                triggered_by=step,
                created_at=utc_now(),
            )
        self._runtime_controls[step.run_id][control.index] = control
        scope = self.background_scope(step.run_id)
        if scope is not None:
            self._background_horizons[scope] = horizon
        else:
            self._adopt_history(horizon)
            if step.run_id != self._history_root:
                self.horizon_for(self._history_root, pending=True)
        return (control.ref,)

    def runtime_controls(
        self, run_id: str, *, refresh: bool = True
    ) -> tuple[ControlRecord, ...]:
        available = self._runtime_controls.setdefault(run_id, {})
        if refresh:
            cursor, additions = self.store.runtime_controls(
                run_id=run_id, after=self._runtime_cursors.get(run_id)
            )
            self._runtime_cursors[run_id] = cursor
            available.update((control.index, control) for control in additions)
        return tuple(available.values())

    def background_scope(self, run_id: str) -> str | None:
        """Find the nearest independently captured history scope."""
        binding = self._active_bindings.get(run_id)
        while binding is not None:
            if binding.run_id in self._background_horizons:
                return binding.run_id
            binding = (
                self._active_bindings.get(binding.parent.run_id)
                if binding.parent
                else None
            )
        return None

    def horizon_for(
        self, run_id: str, *, pending: bool = False
    ) -> RunRef | StepRef | None:
        scope = self.background_scope(run_id)
        if scope is not None:
            horizon = self._background_horizons[scope]
            if pending:
                for control in self.runtime_controls(run_id):
                    if isinstance(control.payload, CompactControlPayload):
                        horizon = control.payload.horizon
                self._background_horizons[scope] = horizon
            return horizon
        if pending:
            controls = self.runtime_controls(run_id)
            root_controls = (
                self.runtime_controls(self._history_root)
                if run_id != self._history_root
                else ()
            )
            for control in (*root_controls, *controls):
                if isinstance(control.payload, CompactControlPayload):
                    self._adopt_history(control.payload.horizon)
            horizon = self._history_horizon
            binding = self._active_bindings[run_id]
            if (
                horizon is not None
                and binding.horizon != horizon
                and not any(
                    isinstance(control.payload, CompactControlPayload)
                    and control.payload.horizon == horizon
                    for control in controls
                )
            ):
                control = self.store.accept_compact_control(
                    run_id=run_id,
                    horizon=horizon,
                    triggered_by=None,
                    created_at=utc_now(),
                )
                self._runtime_controls[run_id][control.index] = control
        return self._history_horizon

    def _adopt_history(self, horizon: RunRef | StepRef) -> None:
        if horizon not in self._history_versions:
            self.message_history().select(horizon)
            self._history_versions.add(horizon)
            self._history_horizon = horizon

    def runtime_values(
        self, binding: BoundRun, *, step: StepRef | None = None
    ) -> dict[str, object]:
        """Select thread and iteration variables from one runtime-owned snapshot."""
        horizon = (
            self._step_horizons[step]
            if step is not None
            else self.horizon_for(binding.run_id, pending=True)
        )
        history = self.message_history().select(horizon)
        return {
            **history_variables(history.far, history.near, binding.settings.recall),
            **self.iteration_values(binding, step=step),
        }

    def iteration_values(
        self, binding: BoundRun, *, step: StepRef | None = None
    ) -> dict[str, object]:
        """Capture iteration history as data, projecting any retained handles."""
        from .iteration import template_value

        views = self._handle_views.setdefault(step, {}) if step is not None else {}

        def project(local: Local) -> object:
            handle = local.value
            if not isinstance(handle, AwaitableHandle):
                return template_value(local)
            if handle.id not in views:
                views[handle.id] = self.store.run_handle_view(handle)
            return views[handle.id]

        return iteration_values(project, captured=binding.captured_iterations)

    def reset_handle_views(self, step: StepRef) -> None:
        """Start a fresh input snapshot when a structural Step evaluates again."""
        self._handle_views.pop(step, None)

    def project_call_locals(
        self,
        locals: Mapping[str, Local],
        runnable: AgicDecl | FlowDecl,
        reference: str,
        *,
        step: StepRef,
    ) -> Mapping[str, Local]:
        """Bind all inline captures before projecting referenced handle views."""
        if not isinstance(runnable, AgicDecl) or not is_generated_ref(reference):
            return locals
        captured = bind_inline_inputs(
            runnable,
            reference,
            {
                name: "Json"
                if isinstance(local.value, AwaitableHandle)
                else _runtime_local_type(local)
                for name, local in locals.items()
            },
        )
        return self.project_handle_locals(
            locals,
            step=step,
            names=[p.name for p in captured.params] + (["_"] if captured.input else []),
        )

    def project_handle_locals(
        self,
        locals: Mapping[str, Local],
        *,
        step: StepRef,
        names: Sequence[str] | None = None,
    ) -> dict[str, Local]:
        """Capture ordinary metadata using one status snapshot per statement."""
        views = self._handle_views.setdefault(step, {})
        result = dict(locals)
        for name, local in locals.items():
            handle = local.value
            if not isinstance(handle, AwaitableHandle):
                continue
            if names is not None and name not in names:
                result.pop(name)
                continue
            if handle.id not in views:
                views[handle.id] = self.store.run_handle_view(handle)
            result[name] = Local(value=views[handle.id], type_name="Json")
        return result

    def recall(
        self,
        step: StepRef | RunRef,
        payload: RecallControlPayload,
        visible: Mapping[RecallTarget, str],
    ) -> tuple[ControlRef, ...]:
        """Reuse pending work or record one new presentation of a resource."""

        run_id = step.run_id if isinstance(step, StepRef) else str(step)
        payload = canonical_recall(payload)
        for pending in reversed(self.runtime_controls(run_id, refresh=False)):
            if (
                not isinstance(pending.payload, RecallControlPayload)
                or pending.payload.target != payload.target
            ):
                continue
            if pending.payload.revision == payload.revision:
                return (pending.ref,)
            break
        else:
            if visible.get(payload.target) == payload.revision:
                return ()
        control = self.store.accept_recall_control(
            run_id=run_id,
            payload=payload,
            triggered_by=step if isinstance(step, StepRef) else None,
            created_at=utc_now(),
        )
        # Advance the read cursor before a model boundary consumes this fact.
        # Otherwise its next refresh would redeliver our already-adopted recall.
        self.runtime_controls(run_id)
        return (control.ref,)

    def next_step(self, run_id: str) -> int:
        """Return the next unused top-level physical step index."""

        steps = self.store.list_steps(run_id=run_id)
        return (
            max(
                (step.index for step in steps if step.parent is None),
                default=-1,
            )
            + 1
        )

    def _restore_model_limits(self, root_run_id: str) -> None:
        runs = self.store.list_run_tree(root_run_id=root_run_id)
        steps_by_run = self.store.list_steps_for_runs(
            run_ids=tuple(run.id for run in runs)
        )
        internal = {
            run.id
            for run in runs
            if (entry := self.store.get_run_control(run_id=run.id, index=0)) is not None
            and isinstance(entry.payload, RunControlPayload)
            and entry.payload.runnable == COMPACT_RUNNABLE
        }
        for step in (
            step
            for steps in steps_by_run.values()
            for step in steps
            if step.kind == "model"
            and (
                step.status == "succeeded"
                or (
                    step.run_id in internal
                    and step.status in {"failed", "canceled"}
                    and isinstance(step.noted, ModelStepNoted)
                    and step.noted.accounting is not None
                )
            )
        ):
            noted = step.noted if isinstance(step.noted, ModelStepNoted) else None
            accounting = noted.accounting if noted is not None else None
            input_tokens = accounting.input_tokens if accounting is not None else None
            output_tokens = accounting.output_tokens if accounting is not None else None
            cost = selected_usd_cost(accounting)
            self._limits.restore(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost=cost,
            )

    @property
    def store(self) -> RunStore:
        return self.executor.store

    def record_prompt_invocations(
        self,
        binding: BoundRun,
        invocations: Sequence[PromptInvocation],
    ) -> None:
        """Persist prompt expansions performed after one Run was accepted."""

        if not invocations:
            return
        self.store.append_prompt_invocations(
            run_id=binding.run_id,
            invocations=invocations,
        )

    @property
    def models(self) -> Sequence[Model]:
        return self.setup.models_effective()

    def validate_child_inputs(
        self,
        binding: BoundRun,
        step: StepRef,
        name: str,
        locals: Mapping[str, Local],
        *,
        state_snapshot: tuple[AgentState, ControlRef] | None = None,
        include_primary: bool = True,
    ) -> AgicDecl | FlowDecl:
        state, _ = state_snapshot or self.state_for_step(step)
        target = resolve_call_target(state, binding.module, name)
        self.require_inactive_runnable(binding, target, action="run")
        runnable = target.executable
        self._validate_child_contract(step, name, runnable)
        locals = self.project_call_locals(locals, runnable, name, step=step)
        _bind_child_input(
            runnable if include_primary else replace(runnable, input=None),
            locals
            if include_primary
            else {name: local for name, local in locals.items() if name != "_"},
            reference=name,
            structs={
                item.name: item for item in state_program(state, target.module).structs
            },
        )
        return runnable

    def condition_templates(
        self,
        binding: BoundRun,
        step: StepRef,
        name: str,
        *,
        state_snapshot: tuple[AgentState, ControlRef],
        dependencies: AgentState | None = None,
    ) -> tuple[str, ...]:
        state, _ = state_snapshot
        target = resolve_call_target(state, binding.module, name)
        runnable = target.executable
        self._validate_child_contract(step, name, runnable)
        if not isinstance(runnable, AgicDecl):
            return ()
        settings = resolve_settings(runnable, target.module, binding.settings)
        templates = [message.content for message in runnable.messages]
        dependencies = dependencies or state
        for setting, kind in (
            (settings.instruct, "instruct"),
            (settings.context, "context"),
        ):
            if setting is not None and setting.name != "none":
                program = state_program(dependencies, setting.module)
                declaration = (
                    program.find_instruct(setting.name)
                    if kind == "instruct"
                    else program.find_context(setting.name)
                )
                if declaration is not None:
                    templates.append(declaration.body)
        return tuple(templates)

    def resolve_public_input(
        self,
        state: AgentState,
        module: str,
        name: str,
        runnable: AgicDecl | FlowDecl,
        raw_input: Mapping[str, object],
    ) -> RunnableInput:
        """Coerce one JSON object through the target module's input contracts."""

        program = state_program(state, module)
        structs = {item.name: item for item in program.structs}
        try:
            input = decode_runnable_input(runnable, raw_input, structs=structs)
        except (ToolangError, TypeError, ValueError) as exc:
            raise _RunRejected(
                str(exc) or type(exc).__name__,
                details={
                    "code": "invalid_runnable_input",
                    "runnable": f"{runnable.kind}:{name}",
                    "expected": runnable_signature(state, module, runnable),
                    "guidance": (
                        "Retry only when available context provides the required "
                        "values; otherwise respond to the user in the normal model "
                        "output with a specific question."
                    ),
                },
            ) from exc
        _validate_inputs(
            program=program,
            runnable=runnable,
            input=input,
        )
        return input

    def active_path(self, binding: BoundRun) -> tuple[BoundRun, ...]:
        """Return this branch's pinned bindings, ordered root to current."""
        path = [binding]
        while binding.parent is not None:
            binding = self._active_bindings[binding.parent.run_id]
            path.append(binding)
        return tuple(reversed(path))

    def active_runnable_identities(self, parent: BoundRun) -> frozenset[str]:
        """History and sibling branches are not part of the active path."""
        return frozenset(_qualified_identity(item) for item in self.active_path(parent))

    def require_inactive_runnable(
        self, parent: BoundRun, target: ResolvedRunnable, *, action: str
    ) -> None:
        if action in {"exec", "_toolang/exec"} and self.can_self_exec(parent, target):
            return
        if target.identity in self.active_runnable_identities(parent):
            raise ToolangError(
                f"{action} cannot call the current or an ancestor runnable: {target.ref}"
            )

    def can_self_exec(self, caller: BoundRun, target: ResolvedRunnable) -> bool:
        """Only a root without active descendants may replace its own binding."""

        return (
            caller.parent is None
            and caller.run_id == caller.root_run_id
            and target.identity == _qualified_identity(caller)
            and all(run_id == caller.run_id for run_id in self._active_bindings)
        )

    def resolve_invocation(
        self,
        parent: BoundRun,
        reference: str,
        *,
        baseline_state: AgentState | None = None,
        authorize: Callable[[ResolvedRunnable], None] | None = None,
        action: str,
        candidate_state: AgentState | None = None,
    ) -> tuple[AgentState, ResolvedRunnable]:
        """Resolve one known target once, preserving inline code and contracts."""
        baseline_state = baseline_state or parent.state
        baseline = resolve_call_target(baseline_state, parent.module, reference)
        self.require_inactive_runnable(parent, baseline, action=action)
        if authorize is not None:
            authorize(baseline)
        if is_generated_ref(baseline.ref):
            return baseline_state, baseline
        contract_state = baseline_state
        contract_runnable = baseline.executable
        if action in {"exec", "_toolang/exec"} and self.can_self_exec(parent, baseline):
            contract_state = parent.state
            contract_runnable = resolve_bound_runnable(
                parent.state, parent.module, _bound_runnable(parent)
            )
        state = candidate_state or self.latest_state()
        try:
            target = resolve_call_target(state, parent.module, reference)
            if target.identity != baseline.identity:
                raise ToolangError("runnable module changed")
            expected = RunnableContract.resolve(
                contract_runnable,
                structs={
                    item.name: item
                    for item in state_program(contract_state, baseline.module).structs
                },
            )
            actual = RunnableContract.resolve(
                target.executable,
                structs={
                    item.name: item
                    for item in state_program(state, target.module).structs
                },
            )
            if expected != actual:
                raise ToolangError("runnable signature changed")
        except (ToolangError, KeyError, ValueError) as exc:
            raise ToolangError(
                f"Cannot bind {baseline.qualified}: baseline {contract_state.revision}, "
                f"candidate {state.revision}: {exc}"
            ) from exc
        return state, target

    def prepare_flow_exec(
        self,
        parent: BoundRun,
        statement: ExecStmt,
        locals: Mapping[str, Local],
        *,
        step: StepRef,
    ) -> tuple[BoundRun, AgicDecl | FlowDecl, dict[str, Local]]:
        """Bind authored input before committing a native same-Run handoff."""
        state, target = self.resolve_invocation(
            parent, statement.runnable, action="exec"
        )
        locals = self.project_call_locals(
            locals, target.executable, target.ref, step=step
        )
        input, provenance = _bind_child_input(
            target.executable,
            locals,
            reference=target.ref,
            structs={
                item.name: item for item in state_program(state, target.module).structs
            },
        )
        binding, successor_locals = self.prepare_execute(
            parent,
            target,
            input,
            control_input=provenance,
            state=state,
            state_ref=parent.state_ref,
        )
        return binding, target.executable, successor_locals

    def prepare_execute(
        self,
        parent: BoundRun,
        target: ResolvedRunnable,
        input: RunnableInput,
        *,
        control_input: CallInput[Value | TypedRef],
        state: AgentState,
        state_ref: ControlRef,
    ) -> tuple[BoundRun, dict[str, Local]]:
        """Prepare a same-Run replacement without committing the transition."""

        self.require_inactive_runnable(parent, target, action="_toolang/exec")
        ancestor = (
            self._active_bindings[parent.parent.run_id]
            if parent.parent is not None
            else None
        )
        binding = replace(
            parent,
            horizon=self.horizon_for(parent.run_id),
            bindings=RunBindings(
                model=parent.bindings.model,
                runnable=target.ref,
            ),
            input=input,
            control_input=control_input,
            state=state,
            state_ref=state_ref,
            module=target.module,
            settings=resolve_settings(
                target.executable,
                target.module,
                ancestor.settings if ancestor is not None else None,
            ),
        )
        binding = self.prepare_resources(binding, target.executable)
        runnable = target.executable
        if isinstance(runnable, AgicDecl):
            runnable = bind_inline_inputs(
                runnable,
                target.ref,
                {
                    name: value.type
                    if isinstance(value, TypedRef)
                    else value_type(value)
                    for name, value in control_input.items()
                },
            )
        return binding, _execute_locals(input, runnable, control_input)

    def commit_execute(
        self,
        binding: BoundRun,
        *,
        triggered_by: StepRef,
        loops: Sequence[tuple[StepRef, LoopStepNoted]] = (),
    ) -> BoundRun:
        """Persist and activate one prepared same-Run runnable replacement."""

        ref = _bound_runnable(binding)
        control = self.store.accept_exec_control(
            run_id=binding.run_id,
            state=binding.state.revision,
            runnable=ref,
            triggered_by=triggered_by,
            input=binding.control_input,
            created_at=utc_now(),
            loops=loops,
        )
        binding = replace(binding, control_index=control.index, state_ref=control.ref)
        self._preceding_controls.append(control.ref)
        self._active_bindings[binding.run_id] = binding
        self._run_outputs.pop(binding.run_id, None)
        self.executor._observe_control(control)
        return binding

    def schedule_time_limit(
        self,
        task: asyncio.Task[RunRecord],
    ) -> asyncio.TimerHandle | None:
        """Cancel the owner task when its root-tree wall-time limit expires."""

        limit = self._limits.limits.time
        if limit is None:
            return None

        def expire() -> None:
            if task.done():
                return
            self._limits.expire_time()
            task.cancel()

        return asyncio.get_running_loop().call_later(limit, expire)

    def require_model_pricing(self, model: Model) -> None:
        """Require price metadata before a cost-limited model call."""

        self._limits.require_pricing(model, self.models)

    def model_accounting(
        self,
        model: Model,
        usage: ModelUsage | None,
    ) -> ModelAccounting | None:
        """Build accounting facts for one completed model call."""

        return build_model_accounting(model, usage)

    def record_model_accounting(
        self,
        model: Model,
        accounting: ModelAccounting | None,
    ) -> None:
        """Add one model accounting result to root-tree totals."""

        try:
            self._limits.record_model(model, accounting)
        except _RunLimitExceeded as exc:
            self._limits.error = str(exc)
            if self._active is not None and self._background_tasks:
                self._active.loop.call_soon(self._active.task.cancel)
            raise

    async def execute(
        self,
        binding: BoundRun,
        runnable: AgicDecl | FlowDecl | CompactSpec,
        *,
        locals: Mapping[str, Local] | None = None,
        output_binding: str | None = "_",
        begun: bool = False,
    ) -> Local:
        """Execute one accepted agic or flow run and emit its lifecycle."""

        from .runs import agic as agic_run
        from .runs import flow as flow_run
        from .runs import compact as compact_run

        if binding.resources is None:
            raise RuntimeError(f"run resources missing: {binding.run_id}")
        entry_binding = binding
        entry_runnable = runnable
        transferred = False
        current = (
            dict(locals)
            if locals is not None
            else {}
            if isinstance(runnable, CompactSpec)
            else initial_locals(binding)
        )
        statement_start = 0
        step_start = self.next_step(binding.run_id)
        self._preceding_controls.append(
            ControlRef.for_run(binding.run_id, binding.control_index)
        )
        if not begun:
            await self.emit(
                RunBegin(
                    run=binding.run_id,
                    control=ControlRef(RunRef(binding.run_id), binding.control_index),
                    runnable=_bound_runnable(binding),
                    parent=binding.parent,
                    occurrence=binding.occurrence,
                    started_at=utc_now(),
                )
            )
        try:
            if (
                self._retry is not None
                and binding.run_id == str(self._retry.target)
                and isinstance(runnable, FlowDecl)
            ):
                current, statement_start = self._resume_flow(
                    binding,
                    runnable,
                    current,
                )
            if self._retry is not None and binding.run_id == str(self._retry.target):
                self._limits.check_restored()
            while True:
                try:
                    try:
                        if isinstance(runnable, CompactSpec):
                            self._limits.check_restored()
                            result = await compact_run.execute(self, binding, runnable)
                        elif isinstance(runnable, AgicDecl):
                            result = await agic_run.execute(
                                self,
                                binding,
                                runnable,
                                current,
                                step_start=step_start,
                            )
                            current["_"] = result
                        else:
                            result = await flow_run.execute(
                                self,
                                binding,
                                runnable,
                                current,
                                statement_start=statement_start,
                                step_start=step_start,
                            )
                        break
                    finally:
                        from .awaitables import drain

                        await drain(self, binding.run_id)
                except _ExecuteCommitted as transfer:
                    binding = transfer.binding
                    runnable = transfer.runnable
                    current = transfer.locals
                    statement_start = 0
                    # A committed handoff is the last Step of the outgoing body.
                    step_start = transfer.triggered_by.indices[0] + 1
                    transferred = True
                    if transfer.interruption is not None and not self.immediate_steer(
                        binding.run_id
                    ):
                        raise transfer.interruption
                    # Pure Flow successors may never suspend. Let cancellation,
                    # time limits, and State publication run between handoffs.
                    await asyncio.sleep(0)
            if transferred:
                assert not isinstance(entry_runnable, CompactSpec)
                result = _coerce_execute_output(
                    entry_binding,
                    entry_runnable,
                    result,
                )
        except asyncio.CancelledError as exc:
            if self._limits.error is not None:
                error = self._limits.error
                await self.emit(
                    RunEnd(
                        run=binding.run_id,
                        status="failed",
                        output=self.run_output(binding.run_id),
                        error=ErrorMessage(error),
                        finished_at=utc_now(),
                    )
                )
                raise _RunLimitExceeded(error) from exc
            control = (
                exc.control
                if isinstance(exc, _RunCanceled)
                else self._active.interruption
                if self._active is not None
                and self._active.interruption is not None
                and self._active.interruption.kind == "cancel"
                else next(
                    (
                        item
                        for item in self.pending_controls(binding.run_id, "cancel")
                        if item.timing == "immediate"
                    ),
                    None,
                )
            )
            await self.emit(
                RunEnd(
                    run=binding.run_id,
                    status="canceled",
                    control=control.ref if control is not None else None,
                    output=self.run_output(binding.run_id),
                    error=ErrorMessage(control_text(control) or "canceled"),
                    finished_at=utc_now(),
                )
            )
            raise
        except _StepFailed as exc:
            await self.emit(
                RunEnd(
                    run=binding.run_id,
                    status="failed",
                    output=self.run_output(binding.run_id),
                    error=exc.error,
                    finished_at=utc_now(),
                )
            )
            raise
        except Exception as exc:
            error = str(exc) or type(exc).__name__
            await self.emit(
                RunEnd(
                    run=binding.run_id,
                    status="failed",
                    output=self.run_output(binding.run_id),
                    error=ErrorMessage(error),
                    finished_at=utc_now(),
                )
            )
            raise
        await self.emit(
            RunEnd(
                run=binding.run_id,
                status="succeeded",
                output=_run_result_output(result, binding=output_binding),
                finished_at=utc_now(),
            )
        )
        return result

    def _resume_flow(
        self,
        binding: BoundRun,
        flow: FlowDecl,
        current: dict[str, Local],
    ) -> tuple[dict[str, Local], int]:
        committed = [
            step
            for step in self.store.list_steps(run_id=binding.run_id)
            if step.parent is None
        ]
        if len(committed) > len(flow.stmts):
            raise ValueError(f"retry prefix exceeds flow body: {binding.run_id}")
        for index, step in enumerate(committed):
            statement = flow.stmts[index]
            if step.given != statement:
                raise ValueError(
                    f"retry prefix no longer matches flow statement: {step.ref}"
                )
            if step.status != "succeeded":
                raise ValueError(f"retry prefix step is not committed: {step.ref}")
            if isinstance(statement, RepeatStmt):
                for descendant in self.store.list_steps(run_id=binding.run_id):
                    if (
                        len(descendant.ref.indices) <= len(step.ref.indices)
                        or descendant.ref.indices[: len(step.ref.indices)]
                        != step.ref.indices
                    ):
                        continue
                    if descendant.status != "succeeded":
                        raise ValueError(
                            f"retry prefix step is not committed: {descendant.ref}"
                        )
                    self._restore_step_local(binding.run_id, descendant, current)
                continue
            if (
                statement.binding is None
                or isinstance(statement, AwaitStmt)
                and step.output is None
            ):
                continue
            local = _step_local(step, self.store)
            current[statement.binding] = local
            if statement.binding == "_":
                self.record_output(
                    binding.run_id,
                    local.ref or FieldRef.from_path(step.ref, "output", "value"),
                )
        return current, len(committed)

    def _restore_step_local(
        self,
        run_id: str,
        step: StepRecord,
        current: dict[str, Local],
    ) -> None:
        """Restore one named local produced inside a committed structural step."""

        if step.output is None or step.output.binding is None:
            return
        local = _step_local(step, self.store)
        current[step.output.binding] = local
        if step.output.binding == "_":
            self.record_output(
                run_id,
                local.ref or FieldRef.from_path(step.ref, "output", "value"),
            )

    async def execute_child(
        self,
        parent: BoundRun,
        locals: Mapping[str, Local],
        step: StepRef,
        name: str,
        occurrence: Occurrence | None,
        *,
        output_binding: str | None = "_",
        resolution: Literal["module", "state"] = "module",
        raw_input: Mapping[str, object] | None = None,
        authorize: Callable[[ResolvedRunnable], None] | None = None,
        state_snapshot: tuple[AgentState, ControlRef] | None = None,
        expected_output: OutputContract | None = None,
        candidate_state: AgentState | None = None,
    ) -> Local:
        """Accept and execute one authored child call."""

        binding, runnable = await self.accept_child(
            parent,
            locals,
            step,
            name,
            occurrence,
            resolution=resolution,
            raw_input=raw_input,
            authorize=authorize,
            state_snapshot=state_snapshot,
            expected_output=expected_output,
            candidate_state=candidate_state,
        )
        result = await self._execute_child_binding(
            binding,
            runnable,
            output_binding=output_binding,
        )
        if expected_output is not None:
            actual_output = _runtime_local_type(result)
            if actual_output != expected_output.type_name:
                raise ToolangError(
                    f"{name!r} requires {expected_output.type_name} output, "
                    f"got {actual_output}"
                )
        return result

    async def accept_child(
        self,
        parent: BoundRun,
        locals: Mapping[str, Local],
        step: StepRef,
        name: str,
        occurrence: Occurrence | None,
        *,
        resolution: Literal["module", "state"] = "module",
        raw_input: Mapping[str, object] | None = None,
        authorize: Callable[[ResolvedRunnable], None] | None = None,
        state_snapshot: tuple[AgentState, ControlRef] | None = None,
        expected_output: OutputContract | None = None,
        candidate_state: AgentState | None = None,
        asynchronous: bool = False,
    ) -> tuple[BoundRun, AgicDecl | FlowDecl]:
        """Validate and commit a child Run before dispatching it."""

        def prepare(
            state: AgentState,
            state_ref: ControlRef,
        ) -> tuple[BoundRun, AgicDecl | FlowDecl]:
            state, target = self.resolve_invocation(
                parent,
                name,
                baseline_state=state if resolution == "state" else parent.state,
                authorize=authorize,
                action="_toolang/run" if resolution == "state" else "run",
                candidate_state=candidate_state,
            )
            runnable = target.executable
            if resolution == "state":
                input = self.resolve_public_input(
                    state,
                    target.module,
                    target.name,
                    runnable,
                    {} if raw_input is None else raw_input,
                )
                return self._prepare_public_child(
                    parent,
                    target.module,
                    target.name,
                    runnable,
                    input,
                    parent_step=step,
                    state=state,
                    state_ref=state_ref,
                ), runnable
            self._validate_child_contract(step, name, runnable)
            if expected_output is not None:
                expected_output.validate(
                    runnable.output or "Part[]",
                    structs={
                        item.name: item
                        for item in state_program(state, target.module).structs
                    },
                    name=name,
                )
            binding = _child_binding(
                self,
                parent,
                target.module,
                target.name,
                runnable,
                locals,
                parent_step=step,
                occurrence=occurrence,
                state=state,
                state_ref=state_ref,
            )
            return self.prepare_resources(binding, runnable), runnable

        binding, runnable = await self._begin_child(
            prepare,
            state_snapshot=state_snapshot or (parent.state, parent.state_ref),
            asynchronous=asynchronous,
        )
        assert not isinstance(runnable, CompactSpec)
        return binding, runnable

    def _prepare_public_child(
        self,
        parent: BoundRun,
        module: str,
        name: str,
        runnable: AgicDecl | FlowDecl,
        input: RunnableInput,
        *,
        parent_step: StepRef,
        state: AgentState,
        state_ref: ControlRef,
        validate_input: bool = True,
    ) -> BoundRun:
        if validate_input:
            _validate_inputs(
                program=state_program(state, module),
                runnable=runnable,
                input=input,
            )
        binding = BoundRun(
            run_id=self.executor.ids.issue_run(),
            root_run_id=parent.root_run_id,
            thread=parent.thread,
            bindings=RunBindings(
                model=parent.bindings.model,
                runnable=f"{runnable.kind}:{name}",
            ),
            model_request=parent.model_request,
            input=input,
            control_input=_snapshot_input(input, runnable),
            state=state,
            state_ref=state_ref,
            setup=parent.setup,
            workspaces=parent.workspaces,
            module=module,
            limits=parent.limits,
            ceilings=parent.ceilings,
            resource_ceiling=parent.resource_ceiling,
            captured_iterations=parent.captured_iterations,
            settings=resolve_settings(runnable, module, parent.settings),
            created_at=utc_now(),
            call="run",
            parent=parent_step,
        )
        return self.prepare_resources(binding, runnable)

    def prepare_resources(
        self,
        binding: BoundRun,
        runnable: AgicDecl | FlowDecl,
        *,
        state: AgentState | None = None,
    ) -> BoundRun:
        """Select fresh resources without changing any accepted code binding."""
        path = self.active_path(binding)
        rules = tuple(
            (
                ancestor.module,
                resolve_bound_runnable(
                    ancestor.state, ancestor.module, _bound_runnable(ancestor)
                ),
            )
            for ancestor in path[:-1]
        ) + ((binding.module, runnable),)
        agent_resources, resources = resolve_path_resources(
            binding.setup,
            state or binding.state,
            rules=rules,
            ceilings=binding.ceilings,
            external=self._agent_resources,
        )
        if binding.resource_ceiling is not None:
            ceiling = binding.resource_ceiling
            resources = replace(
                resources,
                models=tuple(m for m in resources.models if m in ceiling.models),
                tools=tuple(t for t in resources.tools if t in ceiling.tools),
                caps=tuple(c for c in resources.caps if c in ceiling.caps),
            )
        if isinstance(runnable, AgicDecl):
            validate_model_binding(
                self.models,
                runnable=runnable,
                resources=resources,
                model=binding.bindings.model,
            )
        return replace(binding, agent_resources=agent_resources, resources=resources)

    async def _begin_child(
        self,
        prepare: Callable[
            [AgentState, ControlRef],
            tuple[BoundRun, AgicDecl | FlowDecl | CompactSpec],
        ],
        *,
        state_snapshot: tuple[AgentState, ControlRef],
        resume: RunRecord | None = None,
        asynchronous: bool = False,
    ) -> tuple[BoundRun, AgicDecl | FlowDecl | CompactSpec]:
        """Prepare and atomically accept a child before starting any of its work."""

        if resume is not None:
            self._limits = _RunLimitState(self._limits.limits)
            self._restore_model_limits(self._history_root)

        async def accept(
            state: AgentState, state_ref: ControlRef
        ) -> tuple[
            BoundRun,
            AgicDecl | FlowDecl | CompactSpec,
        ]:
            try:
                binding, runnable = prepare(state, state_ref)
                assert binding.parent is not None
                binding = replace(
                    binding,
                    state_ref=resume.state
                    if resume is not None
                    else ControlRef(RunRef(binding.run_id), 0),
                    horizon=self.horizon_for(binding.parent.run_id),
                    cwd=self.cwd_for_run(binding.parent.run_id),
                )
            except (ToolangError, TypeError, ValueError) as exc:
                raise _RunRejected(str(exc) or type(exc).__name__) from exc
            resources = binding.resources
            if resources is None:
                raise RuntimeError(f"run resources missing: {binding.run_id}")
            if asynchronous:
                assert binding.parent is not None
                parent = self._active_bindings[binding.parent.run_id]
                binding = replace(
                    binding,
                    captured_iterations=self.iteration_values(
                        parent, step=binding.parent
                    ),
                )
            try:
                self._active_bindings[binding.run_id] = binding
                if resume is None:
                    from .awaitables import admission

                    admission(self, binding, runnable, asynchronous=asynchronous)
                self._cwd_cache[binding.run_id] = binding.cwd
                self.executor._register_child_run(
                    run_id=binding.run_id,
                    root_run_id=binding.root_run_id,
                )
                dispatch_error: Exception | None = None
                if asynchronous:
                    from .awaitables import start

                    try:
                        start(self, binding, runnable)
                    except Exception as exc:
                        dispatch_error = exc
                event = RunBegin(
                    run=binding.run_id,
                    control=ControlRef(RunRef(binding.run_id), binding.control_index),
                    runnable=_bound_runnable(binding),
                    parent=binding.parent,
                    occurrence=binding.occurrence,
                    started_at=resume.started_at
                    if resume and resume.started_at
                    else utc_now(),
                )
                if self._active is None:
                    emit = self._emit_trace
                    if emit is None:  # pragma: no cover - constructor invariant
                        raise RuntimeError("execution event emitter is missing")
                    await emit(event)
                else:
                    await self.executor._emit_event_locked(self._active, event)
                if dispatch_error is not None:
                    terminal = RunEnd(
                        run=binding.run_id,
                        status="failed",
                        error=ErrorMessage(str(dispatch_error)),
                        finished_at=utc_now(),
                    )
                    if self._active is not None:
                        await self.executor._emit_event_locked(self._active, terminal)
                    elif self._emit_trace is not None:
                        await self._emit_trace(terminal)
            except BaseException as exc:
                if isinstance(exc, asyncio.CancelledError) and (
                    not asynchronous or binding.run_id not in self._background_tasks
                ):
                    # RunBegin is durable before observers are awaited. Close an
                    # accepted child even if cancellation prevents execute().
                    # accept() already owns event_lock, so do not acquire it again.
                    async def emit_terminal(event: RunEvent) -> None:
                        if self._active is not None:
                            await self.executor._emit_event_locked(self._active, event)
                        elif self._emit_trace is not None:
                            await self._emit_trace(event)

                    record = self.store.get_run(run_id=binding.run_id)
                    if record is not None:
                        await self.executor._ensure_terminal(
                            binding.run_id, emit=emit_terminal, status="canceled"
                        )
                if not asynchronous or binding.run_id not in self._background_tasks:
                    self._active_bindings.pop(binding.run_id, None)
                    self._cwd_cache.pop(binding.run_id, None)
                raise
            return binding, runnable

        if self._active is None:
            return await accept(*state_snapshot)
        async with self._active.event_lock:
            return await accept(*state_snapshot)

    async def _execute_child_binding(
        self,
        binding: BoundRun,
        runnable: AgicDecl | FlowDecl,
        *,
        output_binding: str | None = "_",
    ) -> Local:
        resources = binding.resources
        if resources is None:
            raise RuntimeError(f"run resources missing: {binding.run_id}")
        try:
            result = await self.execute(
                binding,
                runnable,
                output_binding=output_binding,
                begun=True,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            child = self.store.get_run(run_id=binding.run_id)
            if child is None or child.status in {"pending", "running"}:
                raise
            raise _ExecutionFailed(
                ErrorRef(FieldRef.from_path(RunRef(binding.run_id), "error")), exc
            ) from exc
        pointer = FieldRef.from_path(RunRef(binding.run_id), "output", "value")
        item_type = result.type_name or "Json"
        source_pointer = (
            result.ref
            if result.ref is not None
            and (result.has_stored or result.type_name == "Part[]")
            else pointer
        )
        return replace(
            result,
            ref=source_pointer,
            stored=value_for_type(
                type_name=item_type,
                value=pointer,
            ),
        )

    def _validate_child_contract(
        self, step: StepRef, name: str, runnable: AgicDecl | FlowDecl
    ) -> None:
        record = self.store.get_step(ref=step)
        if record is not None and not isinstance(
            record.given, StoredModelStepGiven | ToolStepGiven
        ):
            validate_operation_contract(
                record.given.kind,
                runnable,
                name=name,
                line=record.given.span.line,
            )

    async def parallel_children(
        self,
        binding: BoundRun,
        locals: Mapping[str, Local],
        parent: StepRef,
        runnable: str,
        inputs: Sequence[Any],
        *,
        limit: int | None,
        select_source: bool = True,
    ) -> Local:
        """Execute child runs concurrently and preserve their output type."""

        state, state_ref = self.state_for_step(parent)
        lanes = limit or binding.settings.lanes
        available_lanes: asyncio.Queue[int] = asyncio.Queue()
        for lane in range(lanes):
            available_lanes.put_nowait(lane)
        source_local = locals.get("_", Local())
        input_type = source_local.element_type
        target = resolve_call_target(state, binding.module, runnable)
        self.require_inactive_runnable(binding, target, action="run")
        declaration = target.executable
        self._validate_child_contract(parent, runnable, declaration)
        # Later children may adopt publications; the caller keeps its output type.
        output_type = declaration.output or "Part[]"
        structs = {
            item.name: item for item in state_program(state, target.module).structs
        }
        output_contract = OutputContract.resolve(output_type, structs=structs)

        def child_locals(index: int, value: Any) -> dict[str, Local]:
            current = dict(locals)
            if select_source:
                current["_"] = Local(
                    value,
                    ref=source_local.ref.select(index)
                    if source_local.ref is not None
                    else None,
                    type_name=input_type,
                )
            return current

        # Reject known argument failures before any lane can make a model call.
        if not inputs:
            _bind_child_input(
                replace(declaration, input=None) if select_source else declaration,
                {name: local for name, local in locals.items() if name != "_"}
                if select_source
                else locals,
                reference=runnable,
                structs=structs,
            )
        for index, value in enumerate(inputs):
            _bind_child_input(
                declaration,
                child_locals(index, value),
                reference=runnable,
                structs=structs,
            )

        async def execute(index: int, value: Any) -> Local:
            lane = await available_lanes.get()
            try:
                return await self.execute_child(
                    binding,
                    child_locals(index, value),
                    parent,
                    runnable,
                    Occurrence(
                        item=OccurrencePosition(index=index, count=len(inputs)),
                        lane=OccurrencePosition(index=lane, count=lanes),
                    ),
                    expected_output=output_contract,
                )
            except _ExecutionFailed as exc:
                raise RuntimeError(
                    f"parallel step stopped because lane {lane} (#{index}) failed"
                ) from exc
            finally:
                available_lanes.put_nowait(lane)

        tasks = [
            asyncio.create_task(execute(index, value))
            for index, value in enumerate(inputs)
        ]
        try:
            results = list(await asyncio.gather(*tasks))
        except BaseException:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        result_refs = tuple(result.ref for result in results if result.ref is not None)
        return Local(
            [result.value for result in results],
            type_name=f"{output_type}[]",
            stored=(
                value_for_type(
                    type_name=f"{output_type}[]",
                    value=result_refs,
                )
                if len(result_refs) == len(results)
                else _MISSING
            ),
        )

    async def execute_statements(
        self,
        binding: BoundRun,
        statements: Sequence[FlowStmt],
        locals: dict[str, Local],
        *,
        parent: StepRef,
        start: int = 0,
        occurrence: Occurrence | None = None,
    ) -> int:
        """Delegate nested statement execution to the flow run implementation."""

        from .runs.flow import execute_statements

        return await execute_statements(
            self,
            binding,
            statements,
            locals,
            parent=parent,
            start=start,
            occurrence=occurrence,
        )

    def pending_controls(
        self, run_id: str, kind: ControlKind
    ) -> tuple[ControlRecord, ...]:
        return self.executor._pending_controls(run_id=run_id, kind=kind)

    def steer_controls_for_call(
        self,
        run_id: str,
    ) -> tuple[ControlRecord, ...]:
        return self.executor._claim_controls(
            run_id=run_id,
            controls=self.pending_controls(run_id, "steer"),
        )

    def steer_before_next_step(self, run_id: str) -> bool:
        """Return whether a steer replaces the next planned non-model step."""

        return any(
            control.timing in {"immediate", "next_step"}
            for control in self.pending_controls(run_id, "steer")
        )

    def immediate_steer(self, run_id: str) -> bool:
        """Return whether an immediate steer interrupted the active step."""

        # A pending steer must not consume cancellation from an expired limit.
        if self._limits.error is not None:
            return False
        if (
            self._active is not None
            and self._active.interruption is not None
            and self._active.interruption.kind != "steer"
        ):
            return False
        return any(
            control.timing == "immediate"
            for control in self.pending_controls(run_id, "steer")
        )

    def canceled_within(self, run_id: str) -> bool:
        """Whether the interruption cancels this target or one of its descendants."""

        control = self._active.interruption if self._active is not None else None
        return (
            control is not None
            and control.kind == "cancel"
            and run_id in self.store.run_ancestry(run_id=str(control.target))
        )

    def raise_if_canceling(self, run_id: str, *, call: bool) -> None:
        allowed = {"immediate", "next_step"}
        if call:
            allowed.add("next_call")
        control = next(
            (
                item
                for item in self.pending_controls(run_id, "cancel")
                if item.timing in allowed
            ),
            None,
        )
        if control is None:
            return
        claimed = self.executor._claim_controls(
            run_id=run_id,
            controls=(control,),
        )
        if claimed:
            if self._active is not None:
                self._active.interruption = claimed[0]
            raise _RunCanceled(claimed[0])

    def record_output(self, run_id: str, ref: FieldRef) -> None:
        record = (
            self.store.get_run(run_id=str(ref.record))
            if isinstance(ref.record, RunRef)
            else self.store.get_step(ref=ref.record)
            if isinstance(ref.record, StepRef)
            else None
        )
        if record is not None and record.output is not None:
            if isinstance(record.output.value, AwaitableHandle):
                raise ToolangError(
                    "Run handles cannot be returned as runnable results; capture their fields instead"
                )
            self._run_outputs[run_id] = Output(
                TypedRef(ref, record.output.type),
                "_",
            )

    def run_output(self, run_id: str) -> Output | None:
        return self._run_outputs.get(run_id)

    async def emit(self, event: RunEvent) -> None:
        if isinstance(event, StepBegin):
            await self.begin_step(
                lambda _state, state_ref: replace(event, state=state_ref),
                run_id=event.step.run_id,
            )
            return
        if self._active is None:
            emit = self._emit_trace
            if emit is None:  # pragma: no cover - constructor invariant
                raise RuntimeError("execution event emitter is missing")
            await emit(event)
            if isinstance(event, StepEnd):
                self._step_states.pop(event.step, None)
                self._handle_views.pop(event.step, None)
            return
        await self.executor._emit_event(self._active, event)
        if isinstance(event, StepEnd):
            self._step_states.pop(event.step, None)
            self._handle_views.pop(event.step, None)

    def step_starter(
        self, binding: BoundRun
    ) -> Callable[
        [Callable[[AgentState, ControlRef], StepBegin]],
        Awaitable[tuple[AgentState, ControlRef]],
    ]:
        return lambda build: self.begin_step(build, run_id=binding.run_id)

    async def begin_step(
        self,
        build: Callable[[AgentState, ControlRef], StepBegin],
        *,
        run_id: str,
    ) -> tuple[AgentState, ControlRef]:
        """Prepare and persist one step against one serialized State snapshot."""

        if self._active is None:
            emit = self._emit_trace
            if emit is None:  # pragma: no cover - constructor invariant
                raise RuntimeError("execution event emitter is missing")
            state, state_ref = self.state_snapshot(run_id)
            event = build(state, state_ref)
            event = self._step_relations(event)
            await emit(event)
            self._adopt_step_relations(event)
            await self._check_step_cancel(event, emit)
            self._step_states[event.step] = (state, state_ref)
            return state, state_ref
        async with self._active.event_lock:
            state, state_ref = self.state_snapshot(run_id)
            event = build(state, state_ref)
            event = self._step_relations(event)
            await self.executor._emit_event_locked(self._active, event)
            await self._check_step_cancel(
                event,
                lambda end: self.executor._emit_event_locked(
                    cast(_ActiveRun, self._active), end
                ),
            )
            self._step_states[event.step] = (state, state_ref)
        return state, state_ref

    async def _check_step_cancel(self, event: StepBegin, emit: EventEmitter) -> None:
        try:
            self.raise_if_canceling(
                event.step.run_id,
                call=isinstance(event.given, (ModelStepGiven, ToolStepGiven))
                or statement_has_call(event.given),
            )
        except _RunCanceled as exc:
            from .steps.tool import (
                _tool_summary,
                _tool_summary_context,
                canceled_result,
            )

            await emit(
                StepEnd(
                    step=event.step,
                    kind=event.kind,
                    status="canceled",
                    noted=ToolStepNoted(
                        summary=_tool_summary(
                            _tool_summary_context(
                                event.given.call,
                                self.setup.tools().get(event.given.call.name),
                            ),
                            "canceled",
                        )
                    )
                    if isinstance(event.given, ToolStepGiven)
                    else None,
                    output=Output(
                        value_for_type(
                            "ToolResultPart",
                            canceled_result(
                                event.given.call,
                                reason="canceled; operation not executed",
                            ),
                        )
                    )
                    if isinstance(event.given, ToolStepGiven)
                    else None,
                    aborted_by=exc.control.ref,
                    finished_at=utc_now(),
                )
            )
            raise

    def _step_relations(self, event: StepBegin) -> StepBegin:
        """Prepare associations without consuming their live state."""

        binding = self._active_bindings.get(event.step.run_id)
        if binding is not None and binding.bindings.runnable == COMPACT_RUNNABLE:
            return replace(
                event,
                preceded_by=tuple(
                    ref
                    for ref in self._preceding_controls
                    if ref.target == event.step.run
                ),
            )
        if event.kind != "model":
            self.horizon_for(event.step.run_id, pending=True)
        targets = {RunRef(event.step.run_id)}
        if self._active is not None:
            targets.add(RunRef(self._active.root_run_id))
        preceding = [ref for ref in self._preceding_controls if ref.target in targets]
        compacts = tuple(
            control
            for control in self.runtime_controls(
                event.step.run_id, refresh=event.kind != "model"
            )
            if isinstance(control.payload, CompactControlPayload)
            and (event.kind != "model" or control.ref in event.preceded_by)
        )
        if compacts:
            horizon = adopted_horizon(
                self.horizon_for(event.step.run_id), compacts, event.step.run
            )
            self.message_history().select(horizon)
            preceding.extend(control.ref for control in compacts)
        # Root controls precede child-local controls; indexes order each scope.
        refs = tuple(
            sorted(
                set((*preceding, *event.preceded_by)),
                key=lambda ref: (ref.target == event.step.run, ref.index),
            )
        )
        return replace(event, preceded_by=refs)

    def _adopt_step_relations(self, event: StepBegin) -> None:
        """Advance control associations after begin commits, before delivery."""

        self._step_horizons[event.step] = self.horizon_for(event.step.run_id)
        refs = set(event.preceded_by)
        available = self._runtime_controls.get(event.step.run_id, {})
        adopted = tuple(
            control for control in available.values() if control.ref in refs
        )
        if adopted:
            binding = self._active_bindings[event.step.run_id]
            self._active_bindings[event.step.run_id] = replace(
                binding,
                horizon=adopted_horizon(binding.horizon, adopted, event.step.run),
            )
            for control in adopted:
                available.pop(control.index)
        self._preceding_controls = [
            ref for ref in self._preceding_controls if ref not in refs
        ]
        if (
            self._active is not None
            and self._active.interruption is not None
            and self._active.interruption.ref in refs
        ):
            self._active.interruption = None

    def state_for_step(self, step: StepRef) -> tuple[AgentState, ControlRef]:
        """Return the immutable State snapshot captured by one started step."""

        try:
            return self._step_states[step]
        except KeyError as exc:
            if self._active is None:
                return self.state_snapshot(step.run_id)
            raise RuntimeError(f"step State boundary is missing: {step}") from exc


def _child_binding(
    context: _Execution,
    parent: BoundRun,
    module: str,
    effective_name: str,
    runnable: AgicDecl | FlowDecl,
    locals: Mapping[str, Local],
    *,
    parent_step: StepRef,
    occurrence: Occurrence | None,
    state: AgentState,
    state_ref: ControlRef,
) -> BoundRun:
    locals = context.project_call_locals(
        locals, runnable, effective_name, step=parent_step
    )
    structs = {item.name: item for item in state_program(state, module).structs}
    input, control_input = _bind_child_input(
        runnable, locals, reference=effective_name, structs=structs
    )
    return BoundRun(
        run_id=context.executor.ids.issue_run(),
        root_run_id=parent.root_run_id,
        thread=parent.thread,
        bindings=RunBindings(
            model=parent.bindings.model,
            runnable=f"{runnable.kind}:{effective_name}",
        ),
        model_request=parent.model_request,
        input=input,
        control_input=control_input,
        state=state,
        state_ref=state_ref,
        setup=parent.setup,
        workspaces=parent.workspaces,
        module=module,
        limits=parent.limits,
        ceilings=parent.ceilings,
        resource_ceiling=parent.resource_ceiling,
        captured_iterations=parent.captured_iterations,
        settings=resolve_settings(runnable, module, parent.settings),
        created_at=utc_now(),
        call="run",
        parent=parent_step,
        occurrence=occurrence,
    )


def _bind_child_input(
    runnable: AgicDecl | FlowDecl,
    locals: Mapping[str, Local],
    *,
    reference: str,
    structs: Mapping[str, StructDecl],
) -> tuple[RunnableInput, CallInput[Value | TypedRef]]:
    """Validate child arguments and retain compatible input references."""

    if isinstance(runnable, AgicDecl):
        runnable = bind_inline_inputs(
            runnable,
            reference,
            {name: _runtime_local_type(local) for name, local in locals.items()},
        )
    parameters = {"_": runnable.input} if runnable.input is not None else {}
    parameters.update((parameter.name, parameter) for parameter in runnable.params)
    source_locals = {
        name: locals[name]
        for name in parameters
        if name in locals and locals[name].has_value
    }
    for name, local in source_locals.items():
        if isinstance(local.value, AwaitableHandle):
            raise ToolangError(
                f"Run handle {name!r} cannot be passed as an input; capture its fields instead"
            )
    if isinstance(runnable, AgicDecl) and is_generated_ref(reference):
        # Captures already passed their producing boundary. Revalidating them
        # against this module can reinterpret Json strings or foreign structs.
        input = CallInput(
            {name: cast(Value, local.value) for name, local in source_locals.items()}
        )
        validate_runnable_arguments(runnable, input)
    else:
        input = bind_runnable_input(
            runnable,
            {
                name: _argument_value(local, parameters[name])
                for name, local in source_locals.items()
            },
            structs=structs,
        )
    control_input = CallInput(
        {
            name: _child_control_value(
                source_locals[name], parameters[name].type_name or "Part[]", value
            )
            for name, value in input.items()
        }
    )
    return input, control_input


def _argument_value(local: Local, parameter: Parameter) -> Value:
    """Represent one child argument according to its declared value type."""

    if (parameter.type_name or "Part[]") != "Part[]":
        return cast(Value, local.value)
    parts = value_parts(local.value, type_name=local.type_name)
    if parts is not None:
        return parts
    return (TextPart(value_text(local.value)),)


def _bind_run(
    spec: RunSpec,
    *,
    runnable: AgicDecl | FlowDecl,
    run_id: str,
    input: RunnableInput,
    agent_resources: AgentResources,
    resources: AgentResources,
) -> BoundRun:
    if not spec.thread or spec.thread != spec.thread.strip():
        raise ValueError("run spec requires a canonical thread id")
    if spec.bindings.runnable is None:
        raise ValueError("run spec requires a runnable binding")
    runnable_name, runnable_kind = parse_runnable_ref(spec.bindings.runnable)
    module, resolved_runnable = resolve_state_runnable(
        spec.state,
        runnable_name,
        kind=runnable_kind,
    )
    control_input = _snapshot_input(input, runnable)
    return BoundRun(
        run_id=run_id,
        root_run_id=run_id,
        thread=spec.thread,
        bindings=RunBindings(
            runnable=f"{resolved_runnable.kind}:{runnable_name}",
            model=spec.bindings.model or "none",
        ),
        model_request=spec.model_request,
        input=control_input,
        control_input=control_input,
        state=spec.state,
        state_ref=ControlRef(RunRef(run_id), 0),
        setup=spec.setup,
        workspaces=spec.launch_context.workspaces
        if spec.launch_context is not None
        else spec.state.workspaces,
        module=module,
        limits=spec.limits,
        ceilings=spec.ceilings,
        agent_resources=agent_resources,
        resources=resources,
        settings=spec.launch_context.settings
        if spec.launch_context is not None
        else resolve_settings(runnable, module),
        captured_iterations=spec.launch_context.iterations
        if spec.launch_context is not None
        else {},
        resource_ceiling=spec.resource_ceiling,
        created_at=utc_now(),
        horizon=spec.horizon,
        cwd=spec.workdir or "",
    )


def _step_local(step: StepRecord, store: RunStore) -> Local:
    if step.output is None:
        return Local()
    return Local(
        value=store.resolve_value(step.output.value),
        ref=(
            store.resolve_value_pointer(step.output.value)
            if isinstance(step.output.value, TypedRef)
            else FieldRef.from_path(step.ref, "output", "value")
        ),
        type_name=None
        if isinstance(step.output.value, AwaitableHandle)
        else step.output.type,
        stored=step.output.value,
    )


def _prepare_run_spec(
    spec: RunSpec,
) -> tuple[AgicDecl | FlowDecl, RunnableInput, AgentResources, AgentResources]:
    if spec.bindings.runnable is None:
        raise ValueError("run spec requires a runnable binding")
    runnable_name, runnable_kind = parse_runnable_ref(spec.bindings.runnable)
    module, runnable = resolve_state_runnable(
        spec.state,
        runnable_name,
        kind=runnable_kind,
    )
    input = spec.input
    _validate_inputs(
        program=state_program(spec.state, module),
        runnable=runnable,
        input=input,
    )
    agent_resources = resolve_agent_resources(
        spec.setup,
        spec.state,
        AgentCeiling(),
        module=module,
        all_tools=spec.all_tools,
    )
    for ceiling in spec.ceilings:
        agent_resources = apply_agent_ceiling(
            spec.setup,
            spec.state,
            agent_resources,
            ceiling,
            module=module,
        )
    if spec.resource_ceiling is not None:
        agent_resources = AgentResources(
            models=tuple(
                m for m in agent_resources.models if m in spec.resource_ceiling.models
            ),
            tools=tuple(
                t for t in agent_resources.tools if t in spec.resource_ceiling.tools
            ),
            caps=tuple(
                c for c in agent_resources.caps if c in spec.resource_ceiling.caps
            ),
        )
    selection = snapshot_model_selection(spec.setup)
    resources = resolve_runnable_resources(
        selection,
        runnable=runnable,
        base=agent_resources,
        setup=spec.setup,
        state=spec.state,
        module=module,
    )
    _validate_prompt_invocations(spec, resources, module=module)
    if spec.model_request is None:
        validate_model_binding(
            selection,
            runnable=runnable,
            resources=resources,
            model=spec.bindings.model,
        )
    else:
        if spec.bindings.model != spec.model_request.ref:
            raise ValueError("run model request does not match its model binding")
        entry = resolve_model(selection, spec.model_request.ref)
        if entry.ref not in resources.models:
            raise ToolangError(
                f"model ref is outside run resources: {spec.model_request.ref}"
            )
        resolve_model_reasoning(
            entry,
            spec.model_request.reasoning,
        )
    return runnable, input, agent_resources, resources


def _validate_prompt_invocations(
    spec: RunSpec,
    resources: AgentResources,
    *,
    module: str,
) -> None:
    if not spec.prompt_invocations:
        return
    available = {
        cap.ref
        for cap in resource_caps(spec.state, resources, module=module)
        if cap.kind == "prompt"
    }
    for invocation in spec.prompt_invocations:
        if invocation.cap_ref not in available:
            raise ToolangError(f"prompt is outside run resources: {invocation.name}")


def _setup_sandbox(setup: AgentSetup) -> str:
    environment = setup.environment
    if environment is None:
        raise ValueError("agent setup requires an execution environment")
    sandbox = environment.sandbox
    if not sandbox or sandbox != sandbox.strip():
        raise ValueError("agent setup requires a canonical sandbox")
    return sandbox


def _validate_inputs(
    *,
    program: Program,
    runnable: AgicDecl | FlowDecl,
    input: RunnableInput,
) -> None:
    name = runnable.name or f"unnamed {runnable.kind}"
    structs = {item.name: item for item in program.structs}
    params = {param.name: param for param in runnable.params}
    args = {name: value for name, value in input.items() if name != "_"}
    unknown = sorted(set(args) - set(params))
    if unknown:
        joined = ", ".join(unknown)
        raise ValueError(f"unknown named inputs for {name}: {joined}")
    missing = sorted(
        name
        for name, param in params.items()
        if not param.optional and name not in args
    )
    if missing:
        joined = ", ".join(missing)
        raise ValueError(f"missing named inputs for {name}: {joined}")
    if runnable.input is None and "_" in input:
        raise ValueError(f"{name} does not accept primary input")
    if runnable.input is not None and not runnable.input.optional and "_" not in input:
        raise ValueError(f"{name} requires primary input")
    if runnable.input is not None and "_" in input:
        validate_value(
            input["_"],
            runnable.input.type_name or "Part[]",
            structs=structs,
            path="primary input",
        )
    for name, value in args.items():
        validate_value(
            value,
            params[name].type_name or "Part[]",
            structs=structs,
            path=f"named input {name}",
        )


def _run_event_id(event: RunEvent) -> str:
    if isinstance(event, RunBegin | RunEnd):
        return event.run
    return event.step.run_id


def _snapshot_input(
    input: RunnableInput,
    runnable: AgicDecl | FlowDecl,
) -> RunnableInput:
    parameters = {item.name: item for item in runnable.params}
    if runnable.input is not None:
        parameters["_"] = runnable.input
    return CallInput(
        {
            name: cast(
                Value, value_for_type(parameters[name].type_name or "Part[]", value)
            )
            for name, value in input.items()
        }
    )


def _execute_locals(
    input: RunnableInput,
    runnable: AgicDecl | FlowDecl,
    records: CallInput[Value | TypedRef],
) -> dict[str, Local]:
    """Bind replacement inputs while retaining their typed provenance."""

    types = {item.name: item.type_name or "Part[]" for item in runnable.params}
    if runnable.input is not None:
        types["_"] = runnable.input.type_name or "Part[]"
    result: dict[str, Local] = {"_": Local()}
    for name, pointer in records.items():
        result[name] = Local(
            input[name],
            pointer.ref if isinstance(pointer, TypedRef) else None,
            types[name],
            value_for_type(types[name], pointer)
            if not isinstance(pointer, TypedRef)
            else pointer,
        )
    return result


def _child_control_value(
    local: Local, type_name: str, value: Value
) -> Value | TypedRef:
    source_type = _runtime_local_type(local)
    return value_for_type(
        type_name,
        local.ref if local.ref is not None and source_type == type_name else value,
    )


def _runtime_local_type(local: Local) -> str | None:
    if isinstance(local.value, AwaitableHandle):
        return None
    if local.has_stored:
        return (
            local.stored.type
            if isinstance(local.stored, TypedRef)
            else value_type(local.stored)
        )
    if local.type_name is None:
        return None
    return local.type_name


def _resolve_stored_input(
    store: RunStore, input: CallInput[Value | TypedRef]
) -> RunnableInput:
    return CallInput(
        {name: cast(Value, store.resolve_value(value)) for name, value in input.items()}
    )


def _bound_runnable(binding: BoundRun) -> str:
    runnable = binding.bindings.runnable
    if not runnable:
        raise RuntimeError(f"run runnable binding is missing: {binding.run_id}")
    if "::" in runnable:
        return runnable
    if binding.module != "agent" or is_unnamed_ref(runnable):
        return f"{binding.module}::{runnable}"
    return runnable


def _qualified_identity(binding: BoundRun) -> str:
    """Identify a runnable by module and name, independently of kind/revision."""
    if not binding.bindings.runnable:
        raise RuntimeError(f"run runnable binding is missing: {binding.run_id}")
    name, _ = parse_runnable_ref(binding.bindings.runnable)
    return f"{binding.module}::{name}"


def _bound_model(binding: BoundRun) -> str:
    model = binding.bindings.model
    return model or "none"


def _coerce_execute_output(
    entry: BoundRun,
    runnable: AgicDecl | FlowDecl,
    result: Local,
) -> Local:
    """Apply the entry runnable's output contract after same-Run replacement."""

    type_name = runnable.output or (
        "Part[]" if isinstance(runnable, AgicDecl) else "Json"
    )
    program = state_program(entry.state, entry.module)
    value = coerce_output(
        result.value,
        type_name,
        structs={item.name: item for item in program.structs},
    )
    source_type = result.type_name or "Json"
    preserve = source_type == type_name
    return Local(
        value=value,
        ref=result.ref if preserve else None,
        type_name=type_name,
        stored=result.stored if preserve else _MISSING,
    )


def _run_result_output(
    result: Local,
    *,
    binding: str | None = "_",
) -> Output | None:
    if not result.has_value:
        return None
    item_type = result.type_name or "Json"
    reference = (
        result.ref
        if result.ref is not None
        and (result.has_stored or result.type_name == "Part[]")
        else None
    )
    concrete = (
        tuple(result.value)
        if item_type.endswith("[]") and isinstance(result.value, list)
        else result.value
    )
    return Output(
        value_for_type(
            type_name=item_type,
            value=reference if reference is not None else cast(Value, concrete),
        ),
        binding,
    )
