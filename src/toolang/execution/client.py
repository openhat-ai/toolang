"""Transport-neutral run client and its process-local implementation."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from toolang.base.types.message import Message
from toolang.execution.calls import IncludeResolver, resolve_spec
from toolang.execution.events import RunTracer
from toolang.execution.executor import LocalRunHandle, RunExecutor
from toolang.execution.history import RunHistory
from toolang.execution.records import RunControlRecord
from toolang.execution.runnables import parse_runnable_ref
from toolang.execution.schemas import ControlInfo, RunDetail, RunRequest
from toolang.execution.types import ControlTiming
from toolang.setup import AgentSetup
from toolang.state.state import AgentState


class RunHandle(Protocol):
    """One accepted root run that can be awaited through a client."""

    @property
    def run_id(self) -> str: ...

    async def wait(self) -> RunDetail:
        """Wait for the accepted root run to become terminal."""
        ...


class RunClient(Protocol):
    """Caller operations required to start and control runs."""

    async def start(
        self,
        request: RunRequest,
        *,
        tracer: RunTracer | None = None,
    ) -> RunHandle: ...

    async def stop(
        self,
        run_id: str,
        *,
        timing: ControlTiming = "immediate",
        request_id: str | None = None,
        reason: str | None = None,
    ) -> ControlInfo: ...

    async def steer(
        self,
        run_id: str,
        message: Message,
        *,
        timing: ControlTiming = "next_step",
        request_id: str | None = None,
    ) -> ControlInfo: ...

    async def close(self) -> None: ...


SetupSource = Callable[[], AgentSetup]
StateSource = Callable[[], AgentState]
IncludeSource = Callable[[AgentSetup], IncludeResolver]


@dataclass(frozen=True, slots=True)
class _LocalRunClientHandle:
    run_id: str
    _handle: LocalRunHandle = field(repr=False)
    _history: RunHistory = field(repr=False)

    async def wait(self) -> RunDetail:
        await self._handle
        detail = self._history.get_run(self.run_id)
        if detail is None:
            raise RuntimeError(f"run detail missing after completion: {self.run_id}")
        return detail


class LocalRunClient:
    """Resolve caller requests and execute them in the current process."""

    def __init__(
        self,
        executor: RunExecutor,
        *,
        setup: SetupSource,
        state: StateSource,
        include: IncludeSource,
    ) -> None:
        self._executor = executor
        self._setup = setup
        self._state = state
        self._include = include
        self._history = RunHistory(executor.store)
        self._closed = False

    async def start(
        self,
        request: RunRequest,
        *,
        tracer: RunTracer | None = None,
    ) -> RunHandle:
        self._require_open()
        setup = self._setup()
        state = self._state()
        default_runnable = _select_runnable_fallback(
            state,
            request.runnable_fallbacks,
        )
        spec = resolve_spec(
            request.commands,
            request.input,
            setup=setup,
            state=state,
            thread=request.thread,
            default_runnable=default_runnable,
            session_commands=request.session_commands,
            include=self._include(setup),
        )
        handle = self._executor.start(
            spec,
            request_id=request.request_id,
            tracer=tracer,
        )
        return _LocalRunClientHandle(
            run_id=handle.run_id,
            _handle=handle,
            _history=self._history,
        )

    async def stop(
        self,
        run_id: str,
        *,
        timing: ControlTiming = "immediate",
        request_id: str | None = None,
        reason: str | None = None,
    ) -> ControlInfo:
        self._require_open()
        control = self._executor.stop(
            run_id=run_id,
            timing=timing,
            request_id=request_id,
            reason=reason,
        )
        return self._control_info(run_id, control)

    async def steer(
        self,
        run_id: str,
        message: Message,
        *,
        timing: ControlTiming = "next_step",
        request_id: str | None = None,
    ) -> ControlInfo:
        self._require_open()
        control = self._executor.steer(
            run_id=run_id,
            message=message,
            timing=timing,
            request_id=request_id,
        )
        return self._control_info(run_id, control)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._executor.shutdown()

    def _control_info(
        self,
        run_id: str,
        control: RunControlRecord,
    ) -> ControlInfo:
        run = self._executor.store.get_run(run_id=run_id)
        if run is None:
            raise RuntimeError(f"accepted run control has no run: {run_id}")
        return ControlInfo.from_record(run, control)

    def _require_open(self) -> None:
        if self._closed:
            raise RuntimeError("run client is closed")


def _select_runnable_fallback(
    state: AgentState,
    candidates: tuple[str, ...],
) -> str:
    for candidate in candidates:
        name, kind = parse_runnable_ref(candidate)
        if kind == "agic" and (
            name == "default" or state.program.find_agic(name) is not None
        ):
            return candidate
        if kind == "flow" and state.program.find_flow(name) is not None:
            return candidate
        if kind is None and (
            name == "default"
            or state.program.find_agic(name) is not None
            or state.program.find_flow(name) is not None
        ):
            return candidate
    joined = ", ".join(candidates)
    raise ValueError(f"no runnable fallback is available: {joined}")
