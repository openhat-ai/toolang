"""Transport-neutral run client and its process-local implementation."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Protocol

from toolang.base.types.message import Message
from toolang.execution.calls import IncludeResolver, resolve_run_request
from toolang.execution.events import RunTracer
from toolang.execution.executor import LocalRunHandle, RunExecutor
from toolang.execution.history import RunHistory
from toolang.execution.records import RunControlRecord
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
    """Connected caller operations for running and controlling runs."""

    async def connect(self) -> None: ...

    async def run(
        self,
        request: RunRequest,
        *,
        tracer: RunTracer | None = None,
    ) -> RunHandle: ...

    async def cancel(
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

    async def disconnect(self) -> None: ...


SetupSource = Callable[[], AgentSetup]
StateSource = Callable[[], AgentState]
StateRefresh = Callable[[], Awaitable[AgentState]]
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
        refresh_state: StateRefresh | None = None,
        include: IncludeSource,
    ) -> None:
        self._executor = executor
        self._setup = setup
        self._state = state
        self._refresh_state = refresh_state
        self._include = include
        self._history = RunHistory(executor.store)
        self._connected = True

    async def connect(self) -> None:
        self._connected = True

    async def run(
        self,
        request: RunRequest,
        *,
        tracer: RunTracer | None = None,
    ) -> RunHandle:
        self._require_connected()
        setup = self._setup()
        state = (
            await self._refresh_state()
            if self._refresh_state is not None
            else self._state()
        )
        spec = resolve_run_request(
            request,
            setup=setup,
            state=state,
            include=self._include(setup),
        )
        handle = self._executor.run(
            spec,
            request_id=request.request_id,
            tracer=tracer,
        )
        return _LocalRunClientHandle(
            run_id=handle.run_id,
            _handle=handle,
            _history=self._history,
        )

    async def cancel(
        self,
        run_id: str,
        *,
        timing: ControlTiming = "immediate",
        request_id: str | None = None,
        reason: str | None = None,
    ) -> ControlInfo:
        self._require_connected()
        control = self._executor.cancel(
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
        self._require_connected()
        control = self._executor.steer(
            run_id=run_id,
            message=message,
            timing=timing,
            request_id=request_id,
        )
        return self._control_info(run_id, control)

    async def disconnect(self) -> None:
        self._connected = False

    def _control_info(
        self,
        run_id: str,
        control: RunControlRecord,
    ) -> ControlInfo:
        run = self._executor.store.get_run(run_id=run_id)
        if run is None:
            raise RuntimeError(f"accepted run control has no run: {run_id}")
        return ControlInfo.from_record(run, control)

    def _require_connected(self) -> None:
        if not self._connected:
            raise RuntimeError("run client is disconnected")
