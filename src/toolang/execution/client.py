"""Transport-neutral run client and its process-local implementation."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from toolang.base.types.message import Message
from toolang.execution.events import RunTracer
from toolang.execution.executor import LocalRunHandle, RunExecutor
from toolang.execution.history import RunHistory
from toolang.execution.records import ControlRecord
from toolang.execution.schemas import (
    ControlInfo,
    RerunRequest,
    RetryRequest,
    RunDetail,
    RunRequest,
)
from toolang.execution.types import ControlTiming


class RunHandle(Protocol):
    """One accepted root run that can be awaited through a client."""

    @property
    def run_id(self) -> str: ...

    async def wait(self) -> RunDetail:
        """Wait for the accepted root run to become terminal."""
        ...


class RunClient(Protocol):
    """Connectable caller operations for running and controlling runs."""

    async def connect(self) -> None: ...

    async def run(
        self,
        request: RunRequest,
        *,
        tracer: RunTracer | None = None,
    ) -> RunHandle: ...

    async def retry(
        self,
        request: RetryRequest,
        *,
        tracer: RunTracer | None = None,
    ) -> RunHandle: ...

    async def rerun(
        self,
        request: RerunRequest,
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
    """Forward caller requests to an executor in the current process."""

    def __init__(self, executor: RunExecutor) -> None:
        self._executor = executor
        self._history = RunHistory(executor.store)
        self._connected = False

    async def connect(self) -> None:
        self._connected = True

    async def run(
        self,
        request: RunRequest,
        *,
        tracer: RunTracer | None = None,
    ) -> RunHandle:
        self._require_connected()
        handle = self._executor.run(
            request,
            tracer=tracer,
        )
        return self._handle(handle)

    async def retry(
        self,
        request: RetryRequest,
        *,
        tracer: RunTracer | None = None,
    ) -> RunHandle:
        self._require_connected()
        handle = self._executor.retry(
            request,
            tracer=tracer,
        )
        return self._handle(handle)

    async def rerun(
        self,
        request: RerunRequest,
        *,
        tracer: RunTracer | None = None,
    ) -> RunHandle:
        self._require_connected()
        handle = self._executor.rerun(
            request,
            tracer=tracer,
        )
        return self._handle(handle)

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

    def _handle(self, handle: LocalRunHandle) -> RunHandle:
        return _LocalRunClientHandle(
            run_id=handle.run_id,
            _handle=handle,
            _history=self._history,
        )

    def _control_info(
        self,
        run_id: str,
        control: ControlRecord,
    ) -> ControlInfo:
        run = self._executor.store.get_run(run_id=run_id)
        if run is None:
            raise RuntimeError(f"accepted run control has no run: {run_id}")
        return ControlInfo.from_record(run, control)

    def _require_connected(self) -> None:
        if not self._connected:
            raise RuntimeError("run client is disconnected")
