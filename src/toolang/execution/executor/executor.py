"""Run acceptance, control, and recursive execution."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Generator, Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
import logging
from decimal import Decimal, InvalidOperation
import threading
import time
from typing import Any, Literal, cast

from toolang.base.model_settings import apply_model_override
from toolang.base.types.model import ModelOverride, ModelRequest, ModelTarget
from toolang.base.types.policy import AgentCeiling, RunBindings, RunLimits
from toolang.base.types.run import ModelUsage
from toolang.base.types.message import Message, TextPart
from toolang.common.errors import ToolangError
from toolang.common.ids import IdIssuer
from toolang.common.time import utc_now
from toolang.lang.ast import (
    AgicDecl,
    FlowDecl,
    FlowStmt,
    Parameter,
    Program,
    RepeatStmt,
)
from toolang.lang.input import (
    PromptInvocation,
    RunnableInput,
    RunnableInputRaw,
    coerce_output,
    decode_json_input,
    resolve_runnable_input,
    validate_value,
)
from toolang.lang.includes import resolve_file_include
from toolang.lang.types import Value
from toolang.plugin.models.resolution import (
    apply_model_parameters,
)
from toolang.plugin.models.collections import ModelCollection
from toolang.state.state import AgentState, StatePublication, state_program
from toolang.state.watcher import StateRefresh
from toolang.state.cache import agent_revision_dir, validate_agent_revision
from toolang.state.prepare import load_agent_state
from toolang.setup import AgentSetup

from ..accounting import selected_usd_cost
from ..calls import (
    IncludeResolver,
    materialize_model_request,
    resolve_restart_request,
    resolve_run_request,
)
from ..events import RunBegin, RunEnd, RunEvent, RunTracer, StepBegin, StepEnd
from ..records import (
    RunControlPayload,
    run_preparation,
    ControlRecord,
    RunRecord,
    StepRecord,
)
from ..store import RunStore
from ..schemas import RerunRequest, RetryRequest, RunRequest
from ..types import (
    ControlTiming,
    AgentResources,
    ControlRef,
    ErrorMessage,
    ErrorRef,
    FieldRef,
    Local as RecordLocal,
    ControlKind,
    StepRef,
    RunRef,
    ModelStepNoted,
    ModelStepGiven,
    ToolStepGiven,
    Occurrence,
    OccurrencePosition,
    TypedRef,
    RunCommand,
)
from ..runnables import (
    ResolvedRunnable,
    parse_runnable_ref,
    runnable_input_contract,
    resolve_bound_runnable,
    resolve_module_runnable,
    resolve_public_runnable,
    resolve_state_runnable,
)
from .common import (
    BoundRun,
    EventEmitter,
    Local,
    _ExecutionFailed,
    _ExecuteCommitted,
    _RunRejected,
    _StepFailed,
    control_text,
    initial_locals,
    statement_has_call,
    value_parts,
    value_text,
)
from .resources import (
    apply_agent_ceiling,
    resource_caps,
    resolve_agent_resources,
    resolve_runnable_resources,
    snapshot_model_selection,
    validate_model_binding,
)
from .limits import (
    _ModelAccounting,
    _RunLimitExceeded,
    _RunLimitState,
    _model_accounting,
)
from ._persist import _PersistSink

_LOGGER = logging.getLogger(__name__)
_CONTROL_POLL_INTERVAL = 0.05

SetupSource = Callable[[], AgentSetup]
ExecutionState = AgentState | StatePublication
StateSource = Callable[[], ExecutionState]
StateLoad = Callable[[str], ExecutionState]
StateRefreshSource = Callable[[], Awaitable[StateRefresh]]
IncludeSource = Callable[[AgentSetup], IncludeResolver]


def _finish_control_waiter(
    waiter: asyncio.Future[ControlRecord],
    control: ControlRecord,
) -> None:
    if not waiter.done():
        waiter.set_result(control)


class _RunCanceled(asyncio.CancelledError):
    def __init__(self, control: ControlRecord) -> None:
        super().__init__(control_text(control) or "canceled")
        self.control = control


@dataclass(slots=True)
class _ActiveRun:
    task: asyncio.Task[RunRecord]
    tracer: RunTracer | None
    root_run_id: str
    root_setup: AgentSetup
    loop: asyncio.AbstractEventLoop = field(repr=False)
    interruption: ControlRecord | None = None
    controls: dict[str, dict[int, ControlRecord]] = field(
        default_factory=dict,
        repr=False,
    )
    event_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    ended: set[str] = field(default_factory=set, repr=False)
    execution: _Execution | None = field(default=None, repr=False)
    reload_states: dict[int, ExecutionState] = field(default_factory=dict, repr=False)
    reload_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    reload_scheduled: bool = field(default=False, repr=False)
    reload_task: asyncio.Task[None] | None = field(default=None, repr=False)
    runtime_tool_lock: asyncio.Lock = field(
        default_factory=asyncio.Lock,
        repr=False,
    )
    control_waiters: dict[
        tuple[str, int],
        set[asyncio.Future[ControlRecord]],
    ] = field(default_factory=dict, repr=False)


@dataclass(frozen=True, slots=True)
class RunSpec:
    """Immutable inputs required to execute one runnable."""

    setup: AgentSetup
    state: ExecutionState
    thread: str
    bindings: RunBindings
    limits: RunLimits
    model_request: ModelRequest | None = None
    ceilings: tuple[AgentCeiling, ...] = ()
    input: RunnableInput = RunnableInput()
    authored_input: RunnableInputRaw | None = None
    authored_commands: tuple[RunCommand, ...] = ()
    authored_session_commands: tuple[RunCommand, ...] = ()
    prompt_invocations: tuple[PromptInvocation, ...] = ()


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

    def reload(
        self,
        state: ExecutionState,
        *,
        request_id: str | None = None,
    ) -> ControlRecord:
        """Persist an immediate Agent State reload for this run tree."""

        return self.executor.reload(
            run_id=self.run_id,
            state=state,
            request_id=request_id,
        )

    def cancel_control(self, index: int) -> ControlRecord:
        """Revoke one pending reload, steer, or cancel control for this run."""

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
        refresh_state: StateRefreshSource | None = None,
        include: IncludeSource | None = None,
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
        self._refresh_state = refresh_state
        self._include = include
        self._persist = _PersistSink(self.store)
        self._control_poll_interval = _CONTROL_POLL_INTERVAL
        self._active: dict[str, _ActiveRun] = {}
        self._tasks: dict[asyncio.Task[RunRecord], tuple[str, _ActiveRun]] = {}
        self._active_lock = threading.Lock()
        self._monitor_task: asyncio.Task[None] | None = None
        self._control_revision = self.store.latest_run_control_revision()
        self._stopped = False

    def start(self) -> None:
        """Start this executor lifecycle."""

        self._require_available()

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
            model=_bound_model(bound),
            model_request=bound.model_request,
            locals=bound.control_locals,
            sandbox=sandbox,
            occurrence=bound.occurrence,
            request_id=request_id,
            created_at=bound.created_at,
            authored_input=spec.authored_input,
            authored_commands=spec.authored_commands,
            authored_session_commands=spec.authored_session_commands,
            prompt_invocations=spec.prompt_invocations,
        )
        return self._launch(bound, runnable, loop=loop, tracer=tracer)

    def rerun(
        self,
        source: str | RerunRequest,
        *,
        setup: AgentSetup | None = None,
        state: ExecutionState | None = None,
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
            model=_bound_model(bound),
            model_request=bound.model_request,
            locals=bound.control_locals,
            sandbox=sandbox,
            occurrence=bound.occurrence,
            request_id=request_id,
            created_at=bound.created_at,
            authored_input=spec.authored_input,
            authored_commands=spec.authored_commands,
            authored_session_commands=spec.authored_session_commands,
            prompt_invocations=spec.prompt_invocations,
        )
        return self._launch(bound, runnable, loop=loop, tracer=tracer)

    def retry(
        self,
        run_id: str | RetryRequest,
        *,
        setup: AgentSetup | None = None,
        state: ExecutionState | None = None,
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
        bound = replace(bound, control_index=control.index)
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

    def _current_snapshots(self) -> tuple[AgentSetup, ExecutionState]:
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

    def _recorded_state(self, run_id: str) -> ExecutionState:
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
        if (
            not isinstance(state, AgentState | StatePublication)
            or state.revision != revision
        ):
            raise ValueError(f"retry state snapshot is not available: {revision}")
        return state

    def _source_spec(
        self,
        run_id: str,
        *,
        setup: AgentSetup,
        state: ExecutionState,
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
        module, _, ref = preparation.runnable.partition("$")
        runnable, kind = parse_runnable_ref(ref)
        resolved_module, declaration = resolve_state_runnable(
            state, runnable, kind=kind
        )
        if resolved_module != module:
            raise ValueError(f"run runnable module changed: {preparation.runnable}")
        persisted_model_request = preparation.model_request
        persisted_model = (
            None if preparation.model == "none" else ModelRequest(preparation.model)
        )
        selected_model_request = model_request or (
            ModelRequest(model)
            if model is not None
            else persisted_model_request or persisted_model
        )
        default_model_request = setup.defaults.model
        if default_model_request is None:
            fallback = setup.models.effective_default(None)
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
            input=_runnable_input_from_locals(
                self.store,
                preparation.input,
            ),
            authored_input=preparation.authored_input,
            authored_commands=preparation.authored_commands,
            authored_session_commands=preparation.authored_session_commands,
            prompt_invocations=preparation.prompt_invocations,
        )

    def _require_retry_compatible(
        self, run_id: str, state: ExecutionState, *, sandbox: str
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

    @property
    def has_state_refresh(self) -> bool:
        """Return whether model-requested State refresh is available."""

        return self._refresh_state is not None

    def _launch(
        self,
        bound: BoundRun,
        runnable: AgicDecl | FlowDecl,
        *,
        loop: asyncio.AbstractEventLoop,
        tracer: RunTracer | None,
        retry: ControlRecord | None = None,
    ) -> LocalRunHandle:
        task = asyncio.create_task(
            self._execute_owned(bound, runnable, tracer=tracer, retry=retry),
            name=f"toolang-run-{bound.run_id}",
        )
        active = _ActiveRun(
            task=task,
            tracer=tracer,
            root_run_id=bound.root_run_id,
            root_setup=bound.setup,
            loop=loop,
        )
        with self._active_lock:
            self._active[bound.run_id] = active
        self._tasks[task] = (bound.run_id, active)
        task.add_done_callback(self._task_done)
        self._ensure_monitor(bound.setup.layout.name)
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
        self._schedule_reload_application(active)
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
            locals=(RecordLocal.typed("Text", reason, "_", 0),)
            if reason is not None
            else (),
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
            locals=(RecordLocal.typed("Part[]", tuple(message.parts), "_", 0),),
            request_id=request_id,
            created_at=utc_now(),
        )
        self._observe_control(control)
        return control

    def reload(
        self,
        *,
        run_id: str,
        state: ExecutionState,
        request_id: str | None = None,
    ) -> ControlRecord:
        """Persist an immediate State reload for a locally owned run tree."""

        return self._accept_reload(
            run_id=run_id,
            state=state,
            request_id=request_id,
        )

    def _accept_reload(
        self,
        *,
        run_id: str,
        state: ExecutionState,
        request_id: str | None,
        triggered_by: StepRef | None = None,
    ) -> ControlRecord:
        """Persist a reload and retain its process-local State snapshot."""

        self._require_available()
        if not isinstance(state, AgentState | StatePublication):
            raise TypeError("reload requires an Agent State publication")
        with self._active_lock:
            active = self._active.get(run_id)
            if active is None:
                raise ValueError(f"run is not owned by this executor: {run_id}")
            root_run_id = active.root_run_id
            layout = active.root_setup.layout
            expected_dir = agent_revision_dir(
                layout,
                state.revision,
            ).resolve()
            durable_state = (
                state.state if isinstance(state, StatePublication) else state
            )
            state_revision_dir = durable_state.revision_dir
            revision_dir = (
                state_revision_dir.resolve() if state_revision_dir is not None else None
            )
            if revision_dir is None or not revision_dir.is_dir():
                raise ValueError("reload requires a durable Agent State")
            if revision_dir != expected_dir:
                raise ValueError("reload Agent State belongs to another layout")
        try:
            validate_agent_revision(layout, state.revision)
            durable_state = load_agent_state(layout, state.revision)
        except (OSError, KeyError, TypeError, ValueError) as exc:
            raise ValueError("reload requires a durable Agent State") from exc
        expected_state = state.state if isinstance(state, StatePublication) else state
        if durable_state != expected_state:
            raise ValueError("reload Agent State does not match its durable revision")
        with active.reload_lock:
            with self._active_lock:
                if self._active.get(root_run_id) is not active:
                    raise ValueError(f"run is not owned by this executor: {run_id}")
            control = self.store.accept_reload_control(
                run_id=root_run_id,
                state=state.revision,
                request_id=request_id,
                created_at=utc_now(),
                triggered_by=triggered_by,
            )
            with self._active_lock:
                active.reload_states[control.index] = state
            self._observe_control(control)
        return control

    async def model_reload(
        self, *, run_id: str, triggered_by: StepRef
    ) -> dict[str, object]:
        """Refresh and synchronously apply State for one model runtime tool."""

        with self._active_lock:
            active = self._active.get(run_id)
        if active is None:
            raise ValueError(f"run is not owned by this executor: {run_id}")
        if self._refresh_state is None:
            raise ToolangError("Agent State refresh is unavailable in this executor")
        async with active.runtime_tool_lock:
            refreshed = await self._refresh_state()
            execution = active.execution
            if execution is None:
                raise RuntimeError(f"run execution is unavailable: {run_id}")
            current, _current_ref = execution._current_state
            diagnostics = [asdict(item) for item in refreshed.diagnostics]
            if diagnostics:
                return {
                    "applied": False,
                    "from_state": current.revision,
                    "state": current.revision,
                    "control": None,
                    "diagnostics": diagnostics,
                }
            changed = refreshed.publication.revision != current.revision
            control = self._accept_reload(
                run_id=run_id,
                state=refreshed.publication,
                request_id=None,
                triggered_by=triggered_by,
            )
            terminal = await self._wait_for_control(active, control)
            if terminal.status != "applied":
                raise ToolangError(
                    terminal.error
                    or f"State reload control {terminal.status}: "
                    f"{terminal.target}@{terminal.index}"
                )
            return {
                "applied": changed,
                "from_state": current.revision,
                "state": refreshed.publication.revision,
                "control": {
                    "target": str(terminal.target),
                    "index": terminal.index,
                },
                "diagnostics": [],
            }

    def cancel_control(self, *, run_id: str, index: int) -> ControlRecord:
        """Revoke one pending reload, steer, or cancel control."""

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
        reload_tasks = {
            active.reload_task
            for _task, (_run_id, active) in owned
            if active.reload_task is not None
        }
        for task in reload_tasks:
            if not task.done():
                task.cancel()
        if reload_tasks:
            await asyncio.gather(*reload_tasks, return_exceptions=True)
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
        async with active.event_lock:
            await self._emit_event_locked(active, event)

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
            self._persist.on_event(event)
            self._update_control_state(event)
        self._update_cached_control_state(event)
        self._track_active_run(event, active)
        if isinstance(event, RunEnd):
            active.ended.add(event.run)
            if active.execution is not None:
                active.execution._active_bindings.pop(event.run, None)
                active.execution._run_lineages.pop(event.run, None)
        if active.tracer is not None:
            try:
                await active.tracer.on_event(event)
            except Exception:
                _LOGGER.exception("run tracer event handling failed")

    def _update_control_state(self, event: RunEvent) -> None:
        if isinstance(event, RunBegin):
            self.store.finish_run_controls(
                run_id=event.run,
                indexes=(event.control.index,),
                finished_at=event.started_at,
            )
            return
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
        apply_reload: _ActiveRun | None = None
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
                    cancel = active.task
                    loop = active.loop
                if control.kind == "reload":
                    apply_reload = active
            else:
                controls.pop(control.index, None)
                if not controls:
                    active.controls.pop(str(control.target), None)
                if control.kind == "reload":
                    active.reload_states.pop(control.index, None)
                    apply_reload = active
        if cancel is not None and loop is not None and not cancel.done():

            def interrupt() -> None:
                if cancel.done():
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
        if apply_reload is not None:
            self._schedule_reload_application(apply_reload)
        self._notify_control_waiters(control)

    def _notify_control_waiters(self, control: ControlRecord) -> None:
        if control.status == "pending":
            return
        with self._active_lock:
            active = self._active.get(str(control.target))
            if active is None:
                return
            waiters = tuple(
                active.control_waiters.pop((str(control.target), control.index), ())
            )
        for waiter in waiters:
            if not waiter.done():
                active.loop.call_soon_threadsafe(
                    _finish_control_waiter,
                    waiter,
                    control,
                )

    async def _wait_for_control(
        self,
        active: _ActiveRun,
        control: ControlRecord,
    ) -> ControlRecord:
        if control.status != "pending":
            return control
        loop = asyncio.get_running_loop()
        waiter: asyncio.Future[ControlRecord] = loop.create_future()
        key = (str(control.target), control.index)
        with self._active_lock:
            active.control_waiters.setdefault(key, set()).add(waiter)
        terminal = self.store.get_run_control(
            run_id=str(control.target),
            index=control.index,
        )
        if terminal is None:
            with self._active_lock:
                active.control_waiters.get(key, set()).discard(waiter)
            raise RuntimeError(
                f"run control disappeared: {control.target}@{control.index}"
            )
        if terminal.status != "pending":
            self._notify_control_waiters(terminal)
        try:
            return await asyncio.shield(waiter)
        except asyncio.CancelledError:
            try:
                self.cancel_control(
                    run_id=str(control.target),
                    index=control.index,
                )
            except ValueError:
                terminal = self.store.get_run_control(
                    run_id=str(control.target),
                    index=control.index,
                )
                if terminal is not None and terminal.status != "pending":
                    self._notify_control_waiters(terminal)
            await asyncio.shield(waiter)
            raise
        finally:
            with self._active_lock:
                waiters = active.control_waiters.get(key)
                if waiters is not None:
                    waiters.discard(waiter)
                    if not waiters:
                        active.control_waiters.pop(key, None)

    def _schedule_reload_application(self, active: _ActiveRun) -> None:
        with self._active_lock:
            if (
                active.reload_scheduled
                or active.task.done()
                or active.execution is None
            ):
                return
            controls = active.controls.get(active.root_run_id, {})
            candidate = next(
                (
                    control
                    for _index, control in sorted(controls.items())
                    if control.kind == "reload" and control.status == "pending"
                ),
                None,
            )
            if candidate is None or candidate.index not in active.reload_states:
                return
            active.reload_scheduled = True

        def start() -> None:
            task = asyncio.create_task(
                self._apply_reload_controls(active),
                name=f"toolang-reload-{active.root_run_id}",
            )
            with self._active_lock:
                active.reload_task = task

        active.loop.call_soon_threadsafe(start)

    async def _apply_reload_controls(self, active: _ActiveRun) -> None:
        try:
            while True:
                async with active.event_lock:
                    with self._active_lock:
                        execution = active.execution
                        controls = active.controls.get(active.root_run_id, {})
                        candidate = next(
                            (
                                control
                                for _index, control in sorted(controls.items())
                                if control.kind == "reload"
                                and control.status == "pending"
                            ),
                            None,
                        )
                        state = (
                            active.reload_states.get(candidate.index)
                            if candidate is not None
                            else None
                        )
                    if execution is None or candidate is None or state is None:
                        return
                    claimed = self.store.claim_run_controls(
                        run_id=active.root_run_id,
                        indexes=(candidate.index,),
                    )
                    if candidate.index not in claimed:
                        with self._active_lock:
                            controls.pop(candidate.index, None)
                            active.reload_states.pop(candidate.index, None)
                        continue
                    self.store.finish_run_controls(
                        run_id=active.root_run_id,
                        indexes=(candidate.index,),
                        finished_at=utc_now(),
                    )
                    execution._current_state = (
                        state,
                        ControlRef(RunRef(active.root_run_id), candidate.index),
                    )
                    execution._preceding_controls.append(candidate.ref)
                    with self._active_lock:
                        controls.pop(candidate.index, None)
                        active.reload_states.pop(candidate.index, None)
                    terminal = self.store.get_run_control(
                        run_id=active.root_run_id,
                        index=candidate.index,
                    )
                    if terminal is not None:
                        self._observe_control(terminal)
        except Exception as exc:
            error = str(exc) or type(exc).__name__
            with self._active_lock:
                controls = active.controls.get(active.root_run_id, {})
                indexes = tuple(
                    index
                    for index, control in controls.items()
                    if control.kind == "reload" and control.status == "pending"
                )
            self.store.fail_run_controls(
                run_id=active.root_run_id,
                indexes=indexes,
                finished_at=utc_now(),
                error=error,
            )
            for index in indexes:
                terminal = self.store.get_run_control(
                    run_id=active.root_run_id,
                    index=index,
                )
                if terminal is not None:
                    self._observe_control(terminal)
            _LOGGER.exception(
                "State reload application failed run=%s",
                active.root_run_id,
            )
        finally:
            with self._active_lock:
                active.reload_scheduled = False
                active.reload_task = None
            self._schedule_reload_application(active)

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
        self._current_state = (root.state, root.state_ref)
        self._step_states: dict[StepRef, tuple[ExecutionState, ControlRef]] = {}
        self._preceding_controls: list[ControlRef] = []
        self._limits = _RunLimitState(root.limits)
        self._retry = retry
        if retry is not None:
            self._restore_model_limits(root.run_id)
        self._run_outputs: dict[str, RecordLocal] = {}
        self._active_bindings: dict[str, BoundRun] = {root.run_id: root}
        self._run_lineages: dict[str, tuple[str, ...]] = {
            root.run_id: (_bound_runnable(root),)
        }

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
        for step in (
            step
            for steps in steps_by_run.values()
            for step in steps
            if step.kind == "model" and step.status == "succeeded"
        ):
            noted = step.noted if isinstance(step.noted, ModelStepNoted) else None
            input_tokens = (
                noted.accounting.input_tokens
                if noted and noted.accounting
                else noted.tokens.input
                if noted and noted.tokens
                else None
            )
            output_tokens = (
                noted.accounting.output_tokens
                if noted and noted.accounting
                else noted.tokens.output
                if noted and noted.tokens
                else None
            )
            raw_cost = noted.cost if noted is not None else None
            if noted is not None and noted.accounting is not None:
                cost = selected_usd_cost(noted.accounting)
            else:
                try:
                    cost = Decimal(str(raw_cost)) if raw_cost is not None else None
                except InvalidOperation:
                    cost = None
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
    def models(self) -> ModelCollection:
        return self.setup.models

    @property
    def has_state_refresh(self) -> bool:
        """Return whether this execution can refresh Agent State."""

        return self.executor.has_state_refresh

    def refresh_run_binding(
        self,
        binding: BoundRun,
        state: ExecutionState,
        state_ref: ControlRef,
        runnable: AgicDecl | FlowDecl,
        *,
        module: str,
    ) -> BoundRun:
        """Rebind one active run to the resources of a captured State."""

        agent_resources = resolve_agent_resources(
            binding.setup,
            state,
            AgentCeiling(),
            module=module,
        )
        for ceiling in binding.ceilings:
            agent_resources = apply_agent_ceiling(
                binding.setup,
                state,
                agent_resources,
                ceiling,
                module=module,
            )
        flow_resources: AgentResources | None = None
        if (
            not isinstance(runnable, FlowDecl)
            and binding.parent is not None
            and binding.flow_resources is not None
        ):
            parent = self._active_bindings.get(binding.parent.run_id)
            if parent is not None:
                parent_binding = parent
                parent_ref = parent_binding.bindings.runnable
                if parent_ref is None:  # pragma: no cover - bound run invariant
                    raise RuntimeError(
                        f"run runnable binding is missing: {parent_binding.run_id}"
                    )
                current_parent = resolve_bound_runnable(
                    state,
                    parent_binding.module,
                    parent_ref,
                )
                refreshed_parent = self.refresh_run_binding(
                    parent_binding,
                    state,
                    state_ref,
                    current_parent,
                    module=parent_binding.module,
                )
                flow_resources = (
                    refreshed_parent.resources
                    if isinstance(current_parent, FlowDecl)
                    else refreshed_parent.flow_resources
                )
        selection = snapshot_model_selection(binding.setup)
        resources = resolve_runnable_resources(
            selection,
            runnable=runnable,
            base=(
                agent_resources
                if isinstance(runnable, FlowDecl)
                else flow_resources or agent_resources
            ),
            setup=binding.setup,
            state=state,
            module=module,
        )
        validate_model_binding(
            selection,
            runnable=runnable,
            resources=resources,
            model=binding.bindings.model,
        )
        return replace(
            binding,
            state=state,
            state_ref=state_ref,
            module=module,
            agent_resources=agent_resources,
            resources=resources,
            flow_resources=(
                resources if isinstance(runnable, FlowDecl) else flow_resources
            ),
        )

    def resolve_public_input(
        self,
        state: ExecutionState,
        module: str,
        name: str,
        runnable: AgicDecl | FlowDecl,
        raw_input: object,
    ) -> RunnableInput:
        """Coerce one JSON object through the target module's input contracts."""

        program = state_program(state, module)
        structs = {item.name: item for item in program.structs}
        try:
            if not isinstance(raw_input, Mapping):
                raise ValueError("_too/run input must be an object")
            if not all(isinstance(name, str) for name in raw_input):
                raise ValueError("_too/run input field names must be text")
            input_values = cast(Mapping[str, object], raw_input)
            parameters = {item.name: item for item in runnable.params}
            primary = input_values.get("_") if "_" in input_values else None
            if primary is not None and runnable.input is not None:
                primary = decode_json_input(
                    primary,
                    runnable.input.type_name or "Part[]",
                    structs=structs,
                )
            named = {
                name: (
                    decode_json_input(
                        value,
                        parameters[name].type_name or "Part[]",
                        structs=structs,
                    )
                    if name in parameters
                    else value
                )
                for name, value in input_values.items()
                if name != "_"
            }
            input = resolve_runnable_input(
                runnable,
                primary=primary,
                named=named,
                structs=structs,
            )
        except (ToolangError, TypeError, ValueError) as exc:
            raise _RunRejected(
                str(exc) or type(exc).__name__,
                details={
                    "code": "invalid_runnable_input",
                    "runnable": f"{runnable.kind}:{name}",
                    "expected": runnable_input_contract(state, module, runnable),
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

    def require_inactive_runnable(
        self,
        parent: BoundRun,
        target: ResolvedRunnable,
        *,
        action: str,
    ) -> None:
        """Reject a model route already active in this or an ancestor lineage."""

        identity = target.qualified
        run_id: str | None = parent.run_id
        while run_id is not None:
            if identity in self._run_lineages.get(run_id, ()):
                raise ValueError(
                    f"{action} cannot call the current or an ancestor runnable: "
                    f"{target.ref}"
                )
            active = self._active_bindings.get(run_id)
            run_id = (
                active.parent.run_id if active and active.parent is not None else None
            )

    def prepare_execute(
        self,
        parent: BoundRun,
        target: ResolvedRunnable,
        input: RunnableInput,
        *,
        source: FieldRef,
        state: ExecutionState,
        state_ref: ControlRef,
    ) -> tuple[BoundRun, dict[str, Local]]:
        """Prepare a same-Run replacement without committing the transition."""

        self.require_inactive_runnable(parent, target, action="_too/execute")
        agent_resources, resources = self._public_runnable_resources(
            parent,
            target.module,
            target.executable,
            state=state,
        )
        control_locals = _execute_control_locals(input, source=source)
        binding = replace(
            parent,
            bindings=RunBindings(
                model=parent.bindings.model,
                runnable=target.ref,
            ),
            input=input,
            control_locals=control_locals,
            state=state,
            state_ref=state_ref,
            module=target.module,
            agent_resources=agent_resources,
            resources=resources,
            flow_resources=(
                resources if isinstance(target.executable, FlowDecl) else None
            ),
        )
        return binding, _execute_locals(input, target.executable, control_locals)

    def commit_execute(
        self,
        binding: BoundRun,
        *,
        triggered_by: StepRef,
    ) -> BoundRun:
        """Persist and activate one prepared same-Run runnable replacement."""

        lineage = self._run_lineages.get(binding.run_id)
        if lineage is None:  # pragma: no cover - active Run invariant
            raise RuntimeError(f"active Run lineage is missing: {binding.run_id}")
        ref = _bound_runnable(binding)
        control = self.store.accept_execute_control(
            run_id=binding.run_id,
            state=binding.state.revision,
            runnable=ref,
            triggered_by=triggered_by,
            locals=binding.control_locals,
            created_at=utc_now(),
        )
        binding = replace(binding, control_index=control.index)
        self._preceding_controls.append(control.ref)
        self._run_lineages[binding.run_id] = (*lineage, ref)
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

    def require_model_pricing(self, target: ModelTarget) -> None:
        """Require price metadata before a cost-limited model call."""

        self._limits.require_pricing(target, self.models)

    def model_accounting(
        self,
        target: ModelTarget,
        usage: ModelUsage | None,
    ) -> _ModelAccounting:
        """Build accounting facts for one completed model call."""

        return _model_accounting(
            target,
            self.models,
            usage,
        )

    def record_model_accounting(
        self,
        target: ModelTarget,
        accounting: _ModelAccounting,
    ) -> None:
        """Add one model accounting result to root-tree totals."""

        self._limits.record_model(target, accounting)

    async def execute(
        self,
        binding: BoundRun,
        runnable: AgicDecl | FlowDecl,
        *,
        locals: Mapping[str, Local] | None = None,
        output_name: str | None = "_",
        begun: bool = False,
    ) -> Local:
        """Execute one accepted agic or flow run and emit its lifecycle."""

        from .runs import agic as agic_run
        from .runs import flow as flow_run

        if binding.resources is None:
            raise RuntimeError(f"run resources missing: {binding.run_id}")
        entry_binding = binding
        entry_runnable = runnable
        transferred = False
        current = (
            dict(locals) if locals is not None else initial_locals(binding, runnable)
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
                    if isinstance(runnable, AgicDecl):
                        result = await agic_run.execute(
                            self,
                            binding,
                            runnable,
                            current,
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
                except _ExecuteCommitted as transfer:
                    binding = transfer.binding
                    runnable = transfer.runnable
                    current = transfer.locals
                    statement_start = 0
                    step_start = self.next_step(binding.run_id)
                    transferred = True
            if transferred:
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
                output=_run_result_local(result, name=output_name),
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
            if statement.binding is None:
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

        if step.output is None or step.output.name is None:
            return
        local = _step_local(step, self.store)
        current[step.output.name] = local
        if step.output.name == "_":
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
        output_name: str | None = "_",
        resolution: Literal["module", "state"] = "module",
        raw_input: object | None = None,
        authorize: Callable[[ResolvedRunnable], None] | None = None,
        state_snapshot: tuple[ExecutionState, ControlRef] | None = None,
    ) -> Local:
        """Accept and execute one recursive child agic or flow run."""

        def prepare(
            state: ExecutionState,
            state_ref: ControlRef,
        ) -> tuple[BoundRun, AgicDecl | FlowDecl]:
            if resolution == "state":
                runnable_name, runnable_kind = parse_runnable_ref(name)
                target = resolve_public_runnable(
                    state,
                    runnable_name,
                    kind=runnable_kind,
                )
                if authorize is not None:
                    authorize(target)
                self.require_inactive_runnable(parent, target, action="_too/run")
                input = self.resolve_public_input(
                    state,
                    target.module,
                    target.name,
                    target.executable,
                    {} if raw_input is None else raw_input,
                )
                binding = self._prepare_public_child(
                    parent,
                    target.module,
                    target.name,
                    target.executable,
                    input,
                    parent_step=step,
                    state=state,
                    state_ref=state_ref,
                )
                return binding, target.executable
            if resolution != "module":  # pragma: no cover - typed caller invariant
                raise ValueError(f"unknown run resolution: {resolution}")
            parent_ref = parent.bindings.runnable
            if parent_ref is None:  # pragma: no cover - bound run invariant
                raise RuntimeError(f"run runnable binding is missing: {parent.run_id}")
            current_parent = resolve_bound_runnable(
                state,
                parent.module,
                parent_ref,
            )
            current_parent_binding = (
                parent
                if state.revision == parent.state.revision
                and state_ref == parent.state_ref
                else self.refresh_run_binding(
                    parent,
                    state,
                    state_ref,
                    current_parent,
                    module=parent.module,
                )
            )
            runnable_name, runnable_kind = (
                parse_runnable_ref(name)
                if name.startswith(("agic:", "flow:"))
                else (name, None)
            )
            effective_name, runnable = resolve_module_runnable(
                state,
                parent.module,
                runnable_name,
                kind=runnable_kind,
            )
            binding = _child_binding(
                self,
                current_parent_binding,
                parent.module,
                effective_name,
                runnable,
                locals,
                parent_step=step,
                occurrence=occurrence,
                state=state,
                state_ref=state_ref,
            )
            return _prepare_child_run(binding, runnable), runnable

        binding, runnable = await self._begin_child(
            prepare,
            state_snapshot=state_snapshot,
        )
        return await self._execute_child_binding(
            binding,
            runnable,
            output_name=output_name,
        )

    def _prepare_public_child(
        self,
        parent: BoundRun,
        module: str,
        name: str,
        runnable: AgicDecl | FlowDecl,
        input: RunnableInput,
        *,
        parent_step: StepRef,
        state: ExecutionState,
        state_ref: ControlRef,
        validate_input: bool = True,
    ) -> BoundRun:
        if validate_input:
            _validate_inputs(
                program=state_program(state, module),
                runnable=runnable,
                input=input,
            )
        agent_resources, resources = self._public_runnable_resources(
            parent,
            module,
            runnable,
            state=state,
        )
        return BoundRun(
            run_id=self.executor.ids.issue_run(),
            root_run_id=parent.root_run_id,
            thread=parent.thread,
            bindings=RunBindings(
                model=parent.bindings.model,
                runnable=f"{runnable.kind}:{name}",
            ),
            model_request=parent.model_request,
            input=input,
            control_locals=_input_locals(input, runnable),
            state=state,
            state_ref=state_ref,
            setup=parent.setup,
            module=module,
            limits=parent.limits,
            ceilings=parent.ceilings,
            agent_resources=agent_resources,
            resources=resources,
            flow_resources=resources if isinstance(runnable, FlowDecl) else None,
            created_at=utc_now(),
            call="run",
            parent=parent_step,
        )

    def _public_runnable_resources(
        self,
        parent: BoundRun,
        module: str,
        runnable: AgicDecl | FlowDecl,
        *,
        state: ExecutionState,
    ) -> tuple[AgentResources, AgentResources]:
        agent_resources = resolve_agent_resources(
            parent.setup,
            state,
            AgentCeiling(),
            module=module,
        )
        for ceiling in parent.ceilings:
            agent_resources = apply_agent_ceiling(
                parent.setup,
                state,
                agent_resources,
                ceiling,
                module=module,
            )
        selection = snapshot_model_selection(parent.setup)
        resources = resolve_runnable_resources(
            selection,
            runnable=runnable,
            base=agent_resources,
            setup=parent.setup,
            state=state,
            module=module,
        )
        validate_model_binding(
            selection,
            runnable=runnable,
            resources=resources,
            model=parent.bindings.model,
        )
        return agent_resources, resources

    async def _begin_child(
        self,
        prepare: Callable[
            [ExecutionState, ControlRef],
            tuple[BoundRun, AgicDecl | FlowDecl],
        ],
        *,
        state_snapshot: tuple[ExecutionState, ControlRef] | None = None,
    ) -> tuple[BoundRun, AgicDecl | FlowDecl]:
        """Resolve, accept, and begin one child at the latest State boundary."""

        async def accept(
            state: ExecutionState, state_ref: ControlRef
        ) -> tuple[
            BoundRun,
            AgicDecl | FlowDecl,
        ]:
            try:
                binding, runnable = prepare(state, state_ref)
            except (ToolangError, TypeError, ValueError) as exc:
                raise _RunRejected(str(exc) or type(exc).__name__) from exc
            resources = binding.resources
            if resources is None:
                raise RuntimeError(f"run resources missing: {binding.run_id}")
            try:
                self._active_bindings[binding.run_id] = binding
                self._run_lineages[binding.run_id] = (_bound_runnable(binding),)
                self.store.accept_run(
                    run_id=binding.run_id,
                    parent=binding.parent,
                    thread=binding.thread,
                    resources=resources,
                    limits=binding.limits,
                    state=None,
                    runnable=_bound_runnable(binding),
                    model=_bound_model(binding),
                    model_request=binding.model_request,
                    locals=binding.control_locals,
                    sandbox=None,
                    occurrence=binding.occurrence,
                    request_id=None,
                    created_at=binding.created_at,
                    state_ref=binding.state_ref,
                )
                self.executor._register_child_run(
                    run_id=binding.run_id,
                    root_run_id=binding.root_run_id,
                )
                event = RunBegin(
                    run=binding.run_id,
                    control=ControlRef(RunRef(binding.run_id), binding.control_index),
                    runnable=_bound_runnable(binding),
                    parent=binding.parent,
                    occurrence=binding.occurrence,
                    started_at=utc_now(),
                )
                if self._active is None:
                    emit = self._emit_trace
                    if emit is None:  # pragma: no cover - constructor invariant
                        raise RuntimeError("execution event emitter is missing")
                    await emit(event)
                else:
                    await self.executor._emit_event_locked(self._active, event)
            except BaseException:
                self._active_bindings.pop(binding.run_id, None)
                self._run_lineages.pop(binding.run_id, None)
                raise
            return binding, runnable

        if self._active is None:
            state, state_ref = state_snapshot or self._current_state
            return await accept(state, state_ref)
        async with self._active.event_lock:
            state, state_ref = state_snapshot or self._current_state
            return await accept(state, state_ref)

    async def _execute_child_binding(
        self,
        binding: BoundRun,
        runnable: AgicDecl | FlowDecl,
        *,
        output_name: str | None = "_",
    ) -> Local:
        resources = binding.resources
        if resources is None:
            raise RuntimeError(f"run resources missing: {binding.run_id}")
        try:
            result = await self.execute(
                binding,
                runnable,
                output_name=output_name,
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
            and (result.record is not None or result.type_name == "Part[]")
            else pointer
        )
        return replace(
            result,
            ref=source_pointer,
            record=RecordLocal.typed(
                type_name=(f"{item_type}[]" if result.shape == "list" else item_type),
                value=pointer,
                dim=1 if result.shape == "list" else 0,
            ),
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

        lanes = limit or max(len(inputs), 1)
        available_lanes: asyncio.Queue[int] = asyncio.Queue()
        for lane in range(lanes):
            available_lanes.put_nowait(lane)
        source_local = locals.get("_", Local())
        input_type = source_local.type_name

        async def execute(index: int, value: Any) -> Local:
            lane = await available_lanes.get()
            try:
                child_locals = dict(locals)
                child_locals["_"] = Local(
                    value,
                    "item",
                    ref=(
                        (
                            source_local.ref.select(index)
                            if select_source
                            else source_local.ref
                        )
                        if source_local.ref is not None
                        else None
                    ),
                    type_name=input_type,
                )
                return await self.execute_child(
                    binding,
                    child_locals,
                    parent,
                    runnable,
                    Occurrence(
                        item=OccurrencePosition(index=index, count=len(inputs)),
                        lane=OccurrencePosition(index=lane, count=lanes),
                    ),
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
        output_type = _parallel_output_type(results) or "Json"
        result_refs = tuple(result.ref for result in results if result.ref is not None)
        return Local(
            [result.value for result in results],
            "list",
            type_name=output_type,
            record=(
                RecordLocal.typed(
                    type_name=f"{output_type}[]",
                    value=result_refs,
                    dim=1,
                )
                if len(result_refs) == len(results)
                else None
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

        return any(
            control.timing == "immediate"
            for control in self.pending_controls(run_id, "steer")
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
        step = next(
            (
                item
                for item in reversed(self.store.list_steps(run_id=run_id))
                if FieldRef.from_path(item.ref, "output", "value") == ref
                and item.output is not None
            ),
            None,
        )
        if step is not None and step.output is not None:
            self._run_outputs[run_id] = replace(
                step.output,
                value=TypedRef(ref, step.output.type),
                name="_",
            )

    def run_output(self, run_id: str) -> RecordLocal | None:
        return self._run_outputs.get(run_id)

    async def emit(self, event: RunEvent) -> None:
        if isinstance(event, StepBegin):
            await self.begin_step(
                lambda _state, state_ref: replace(event, state=state_ref)
            )
            return
        if self._active is None:
            emit = self._emit_trace
            if emit is None:  # pragma: no cover - constructor invariant
                raise RuntimeError("execution event emitter is missing")
            await emit(event)
            if isinstance(event, StepEnd):
                self._step_states.pop(event.step, None)
            return
        await self.executor._emit_event(self._active, event)
        if isinstance(event, StepEnd):
            self._step_states.pop(event.step, None)

    async def begin_step(
        self,
        build: Callable[[ExecutionState, ControlRef], StepBegin],
    ) -> tuple[ExecutionState, ControlRef]:
        """Prepare and persist one step against one serialized State snapshot."""

        if self._active is None:
            emit = self._emit_trace
            if emit is None:  # pragma: no cover - constructor invariant
                raise RuntimeError("execution event emitter is missing")
            state, state_ref = self._current_state
            event = build(state, state_ref)
            event = self._step_relations(event)
            await emit(event)
            await self._check_step_cancel(event, emit)
            self._step_states[event.step] = (state, state_ref)
            return state, state_ref
        async with self._active.event_lock:
            state, state_ref = self._current_state
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
            await emit(
                StepEnd(
                    step=event.step,
                    kind=event.kind,
                    status="canceled",
                    aborted_by=exc.control.ref,
                    finished_at=utc_now(),
                )
            )
            raise

    def _step_relations(self, event: StepBegin) -> StepBegin:
        targets = {RunRef(event.step.run_id)}
        if self._active is not None:
            targets.add(RunRef(self._active.root_run_id))
        preceding = [ref for ref in self._preceding_controls if ref.target in targets]
        self._preceding_controls = [
            ref for ref in self._preceding_controls if ref.target not in targets
        ]
        refs = tuple(dict.fromkeys((*preceding, *event.preceded_by)))
        if (
            self._active is not None
            and self._active.interruption is not None
            and self._active.interruption.ref in refs
        ):
            self._active.interruption = None
        return replace(event, preceded_by=refs)

    def state_for_step(self, step: StepRef) -> tuple[ExecutionState, ControlRef]:
        """Return the immutable State snapshot captured by one started step."""

        try:
            return self._step_states[step]
        except KeyError as exc:
            if self._active is None:
                return self._current_state
            raise RuntimeError(f"step State boundary is missing: {step}") from exc


def _parallel_output_type(
    results: Sequence[Local],
) -> str | None:
    actual = {result.type_name for result in results if result.type_name is not None}
    if len(actual) == 1:
        return next(iter(actual))
    return None


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
    state: ExecutionState,
    state_ref: ControlRef,
) -> BoundRun:
    structs = {item.name: item for item in state_program(state, module).structs}
    source_locals: dict[str, Local] = {}
    primary_value: object | None = None
    if runnable.input is not None:
        primary = locals.get("_", Local())
        if primary.shape != "none":
            primary_value = _argument_value(primary, runnable.input)
            source_locals["_"] = primary
    named: dict[str, object] = {}
    for parameter in runnable.params:
        local = locals.get(parameter.name)
        if local is None or local.shape == "none":
            continue
        named[parameter.name] = _argument_value(local, parameter)
        source_locals[parameter.name] = local
    input = resolve_runnable_input(
        runnable,
        primary=primary_value,
        named=named,
        structs=structs,
    )
    declared_types = {
        **(
            {"_": runnable.input.type_name or "Part[]"}
            if runnable.input is not None
            else {}
        ),
        **{
            parameter.name: parameter.type_name or "Part[]"
            for parameter in runnable.params
        },
    }
    control_locals: list[RecordLocal] = []
    resolved_values = {
        **({"_": input.primary} if input.primary is not None else {}),
        **input.named,
    }
    for name, value in resolved_values.items():
        control_locals.append(
            _child_control_local(
                name,
                source_locals[name],
                declared_types[name],
                cast(Value, value),
            )
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
        control_locals=tuple(control_locals),
        state=state,
        state_ref=state_ref,
        setup=parent.setup,
        module=module,
        limits=parent.limits,
        ceilings=parent.ceilings,
        agent_resources=parent.agent_resources,
        resources=None,
        flow_resources=parent.flow_resources,
        created_at=utc_now(),
        call="run",
        parent=parent_step,
        occurrence=occurrence,
    )


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
    control_locals = _input_locals(input, runnable)
    return BoundRun(
        run_id=run_id,
        root_run_id=run_id,
        thread=spec.thread,
        bindings=RunBindings(
            runnable=f"{resolved_runnable.kind}:{runnable_name}",
            model=spec.bindings.model or "none",
        ),
        model_request=spec.model_request,
        input=_runnable_input_from_values(control_locals),
        control_locals=control_locals,
        state=spec.state,
        state_ref=ControlRef(RunRef(run_id), 0),
        setup=spec.setup,
        module=module,
        limits=spec.limits,
        ceilings=spec.ceilings,
        agent_resources=agent_resources,
        resources=resources,
        flow_resources=resources if isinstance(runnable, FlowDecl) else None,
        created_at=utc_now(),
    )


def _step_local(step: StepRecord, store: RunStore) -> Local:
    if step.output is None:
        return Local()
    return Local(
        value=store.resolve_value(step.output.value),
        shape="list" if step.output.dim == 1 else "item",
        ref=(
            store.resolve_value_pointer(step.output.value)
            if isinstance(step.output.value, TypedRef)
            else FieldRef.from_path(step.ref, "output", "value")
        ),
        type_name=step.output.item_type,
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
    )
    for ceiling in spec.ceilings:
        agent_resources = apply_agent_ceiling(
            spec.setup,
            spec.state,
            agent_resources,
            ceiling,
            module=module,
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
        entry = selection.resolve(spec.model_request.ref)
        if entry.key not in resources.models:
            raise ToolangError(
                f"model ref is outside run resources: {spec.model_request.ref}"
            )
        target = entry.target
        apply_model_parameters(selection, target, spec.model_request.parameters)
    return runnable, input, agent_resources, resources


def _validate_prompt_invocations(
    spec: RunSpec,
    resources: AgentResources,
    *,
    module: str,
) -> None:
    if not isinstance(spec.state, StatePublication) or not spec.prompt_invocations:
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


def _prepare_child_run(
    binding: BoundRun,
    runnable: AgicDecl | FlowDecl,
) -> BoundRun:
    agent_resources = binding.agent_resources
    if agent_resources is None:
        raise RuntimeError(f"agent resources missing: {binding.run_id}")
    base = (
        agent_resources
        if isinstance(runnable, FlowDecl)
        else binding.flow_resources or agent_resources
    )
    selection = snapshot_model_selection(binding.setup)
    resources = resolve_runnable_resources(
        selection,
        runnable=runnable,
        base=base,
        setup=binding.setup,
        state=binding.state,
        module=binding.module,
    )
    return replace(
        binding,
        resources=resources,
        flow_resources=(
            resources if isinstance(runnable, FlowDecl) else binding.flow_resources
        ),
    )


def _validate_inputs(
    *,
    program: Program,
    runnable: AgicDecl | FlowDecl,
    input: RunnableInput,
) -> None:
    structs = {item.name: item for item in program.structs}
    params = {param.name: param for param in runnable.params}
    args = input.named
    unknown = sorted(set(args) - set(params))
    if unknown:
        joined = ", ".join(unknown)
        raise ValueError(f"unknown named inputs for {runnable.name}: {joined}")
    missing = sorted(
        name
        for name, param in params.items()
        if not param.optional and name not in args
    )
    if missing:
        joined = ", ".join(missing)
        raise ValueError(f"missing named inputs for {runnable.name}: {joined}")
    if runnable.input is None and input.primary is not None:
        raise ValueError(f"{runnable.name} does not accept primary input")
    if (
        runnable.input is not None
        and not runnable.input.optional
        and input.primary is None
    ):
        raise ValueError(f"{runnable.name} requires primary input")
    if runnable.input is not None and input.primary is not None:
        validate_value(
            input.primary,
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


def _input_locals(
    input: RunnableInput,
    runnable: AgicDecl | FlowDecl,
) -> tuple[RecordLocal, ...]:
    parameters = {item.name: item for item in runnable.params}
    result: list[RecordLocal] = []
    if runnable.input is not None and input.primary is not None:
        type_name = runnable.input.type_name or "Part[]"
        result.append(
            RecordLocal.typed(
                type_name=type_name,
                value=input.primary,
                name="_",
            )
        )
    for name, item in input.named.items():
        result.append(
            RecordLocal.typed(
                type_name=parameters[name].type_name or "Part[]",
                value=item,
                name=name,
            )
        )
    return tuple(result)


def _execute_control_locals(
    input: RunnableInput,
    *,
    source: FieldRef,
) -> tuple[RecordLocal, ...]:
    """Point raw replacement inputs into the originating Model ToolCall."""

    result: list[RecordLocal] = []
    if input.primary is not None:
        pointer = source.select("input", "input", "_")
        result.append(RecordLocal.typed("Json", pointer, "_"))
    for name in input.named:
        pointer = source.select("input", "input", name)
        result.append(RecordLocal.typed("Json", pointer, name))
    return tuple(result)


def _execute_locals(
    input: RunnableInput,
    runnable: AgicDecl | FlowDecl,
    records: tuple[RecordLocal, ...],
) -> dict[str, Local]:
    """Bind concrete replacement values to their durable model-output sources."""

    types = {item.name: item.type_name or "Part[]" for item in runnable.params}
    if runnable.input is not None:
        types["_"] = runnable.input.type_name or "Part[]"
    values = {
        **({"_": input.primary} if input.primary is not None else {}),
        **input.named,
    }
    result: dict[str, Local] = {"_": Local()}
    for record in records:
        if record.name is None or not isinstance(record.value, TypedRef):
            raise RuntimeError("execute input local is not a named pointer")
        result[record.name] = Local(
            values[record.name],
            "item",
            record.value.ref,
            types[record.name],
            record,
        )
    return result


def _child_control_local(
    name: str,
    local: Local,
    type_name: str,
    value: Value,
) -> RecordLocal:
    source_type = _runtime_local_type(local)
    return RecordLocal.typed(
        type_name=type_name,
        value=(
            local.ref if local.ref is not None and source_type == type_name else value
        ),
        name=name,
        dim=0,
    )


def _runtime_local_type(local: Local) -> str | None:
    if local.record is not None:
        return local.record.type
    if local.type_name is None:
        return None
    return f"{local.type_name}[]" if local.shape == "list" else local.type_name


def _runnable_input_from_locals(
    store: RunStore,
    locals: Sequence[RecordLocal],
) -> RunnableInput:
    primary: Value | None = None
    named: dict[str, Value] = {}
    for local in locals:
        value = cast(Value, store.resolve_value(local.value))
        if local.name == "_":
            primary = value
        elif local.name is not None:
            named[local.name] = value
    return RunnableInput(primary=primary, named=named)


def _runnable_input_from_values(
    locals: Sequence[RecordLocal],
) -> RunnableInput:
    primary: Value | None = None
    named: dict[str, Value] = {}
    for local in locals:
        if isinstance(local.value, TypedRef):
            raise TypeError("top-level input local cannot be a pointer")
        if local.name == "_":
            primary = local.value
        elif local.name is not None:
            named[local.name] = local.value
    return RunnableInput(primary=primary, named=named)


def _bound_runnable(binding: BoundRun) -> str:
    runnable = binding.bindings.runnable
    if not runnable:
        raise RuntimeError(f"run runnable binding is missing: {binding.run_id}")
    return f"{binding.module}${runnable}"


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
    source_type = (
        f"{result.type_name or 'Json'}[]"
        if result.shape == "list"
        else result.type_name or "Json"
    )
    preserve = source_type == type_name
    return Local(
        value=value,
        shape="item",
        ref=result.ref if preserve else None,
        type_name=type_name,
        record=result.record if preserve else None,
    )


def _run_result_local(
    result: Local,
    *,
    name: str | None = "_",
) -> RecordLocal | None:
    if result.shape == "none":
        return None
    item_type = result.type_name or "Json"
    reference = (
        result.ref
        if result.ref is not None
        and (result.record is not None or result.type_name == "Part[]")
        else None
    )
    concrete = (
        tuple(result.value)
        if item_type.endswith("[]") and isinstance(result.value, list)
        else result.value
    )
    return RecordLocal.typed(
        type_name=f"{item_type}[]" if result.shape == "list" else item_type,
        value=reference if reference is not None else cast(Value, concrete),
        name=name,
        dim=1 if result.shape == "list" else 0,
    )
