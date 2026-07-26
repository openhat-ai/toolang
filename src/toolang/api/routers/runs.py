"""Run execution, inspection, control, and live event routes."""

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.sse import EventSourceResponse, ServerSentEvent

from toolang.api.app import AgentCoreDep, LiveEventRelayDep
from toolang.api.common import EventSubscription, sse_stream
from toolang.api.conversion import parse_percept, parse_user_message
from toolang.api.schemas import (
    RunCancelRequest,
    RunCommandResult,
    RunCreateRequest,
    RunSteerRequest,
)
from toolang.common.errors import ToolangError
from toolang.execution.executor import RunHandle, RunSpec
from toolang.execution.records import RunRecord
from toolang.execution.schemas import RunControlInfo, RunDetail, RunInfo
from toolang.execution.types import RunStatus
from toolang.up import AgentCore

router = APIRouter(prefix="/runs", tags=["runs"])
_StartedRunStream = tuple[RunHandle, EventSubscription]


async def _start_run_stream(
    core: AgentCoreDep,
    live: LiveEventRelayDep,
    payload: RunCreateRequest,
) -> AsyncIterator[_StartedRunStream]:
    thread_id = _run_thread(core, payload)
    try:
        handle = core.executor.start(
            RunSpec(
                setup=core.setup.current(),
                state=core.state.current(),
                thread=thread_id,
                runnable=payload.runnable,
                input=parse_percept(payload.input),
                model=payload.model,
                args=payload.args,
            ),
            request_id=payload.request_id,
            tracer=live.trace(thread_id=thread_id),
        )
    except (ToolangError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    subscription = live.subscribe_run(handle.run_id)
    try:
        yield handle, subscription
    finally:
        subscription.close()


async def _subscribe_root_run(
    core: AgentCoreDep,
    live: LiveEventRelayDep,
    run_id: str,
) -> AsyncIterator[EventSubscription]:
    run = _run_or_404(core, run_id)
    if run.parent is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"run stream requires a root run: {run_id}; "
                f"subscribe to {run.root_run_id}"
            ),
        )
    subscription = live.subscribe_run(run_id)
    try:
        yield subscription
    finally:
        subscription.close()


@router.get("", summary="List Runs", response_model=list[RunInfo])
def runs(
    core: AgentCoreDep,
    limit: int = Query(default=50),
    thread_id: str | None = None,
    status: RunStatus | None = None,
) -> list[RunInfo]:
    return core.history.list_runs(
        limit=limit,
        thread_id=thread_id,
        status=status,
    )


@router.post(
    "/stream",
    summary="Execute Run Stream",
    response_class=EventSourceResponse,
)
async def execute_run_stream(
    core: AgentCoreDep,
    request: Request,
    started: Annotated[_StartedRunStream, Depends(_start_run_stream)],
) -> AsyncIterator[ServerSentEvent]:
    handle, subscription = started
    async for event in sse_stream(
        request,
        subscription,
        terminal_run_id=handle.run_id,
        stopped=lambda: _run_terminal(core, handle.run_id),
    ):
        yield event


@router.get("/{run_id}", summary="Get Run", response_model=RunDetail)
def run_detail(core: AgentCoreDep, run_id: str) -> RunDetail:
    detail = core.history.get_run(run_id)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"run not found: {run_id}")
    return detail


@router.get(
    "/{run_id}/stream",
    summary="Stream Run Events",
    response_class=EventSourceResponse,
)
async def run_stream(
    core: AgentCoreDep,
    request: Request,
    run_id: str,
    subscription: Annotated[EventSubscription, Depends(_subscribe_root_run)],
) -> AsyncIterator[ServerSentEvent]:
    async for event in sse_stream(
        request,
        subscription,
        terminal_run_id=run_id,
        stopped=lambda: _run_terminal(core, run_id),
    ):
        yield event


@router.post(
    "/{run_id}/cancel",
    summary="Cancel Run",
    response_model=RunCommandResult,
)
def cancel_run(
    core: AgentCoreDep,
    run_id: str,
    payload: RunCancelRequest | None = None,
) -> RunCommandResult:
    run = _running_run_or_409(core, run_id)
    control = core.executor.stop(
        run_id=run.id,
        timing=payload.mode if payload else "immediate",
        request_id=payload.request_id if payload else None,
        reason=payload.reason if payload else None,
    )
    return _control_result(core, run.id, control)


@router.post(
    "/{run_id}/steer",
    summary="Steer Run",
    status_code=202,
    response_model=RunCommandResult,
)
def steer_run(
    core: AgentCoreDep,
    run_id: str,
    payload: RunSteerRequest,
) -> RunCommandResult:
    run = _running_run_or_409(core, run_id)
    control = core.executor.steer(
        run_id=run.id,
        timing=payload.mode,
        request_id=payload.request_id,
        message=parse_user_message(payload.message),
    )
    return _control_result(core, run.id, control)


def _run_or_404(core: AgentCore, run_id: str) -> RunRecord:
    run = core.store.get_run(run_id=run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"run not found: {run_id}")
    return run


def _running_run_or_409(core: AgentCore, run_id: str) -> RunRecord:
    run = _run_or_404(core, run_id)
    if run.status != "running":
        raise HTTPException(status_code=409, detail=f"run is not running: {run_id}")
    return run


def _control_result(core: AgentCore, run_id: str, control) -> RunCommandResult:
    run = _run_or_404(core, run_id)
    detail = core.history.get_run(run_id)
    if detail is None:
        raise HTTPException(
            status_code=500,
            detail=f"run not found after control: {run_id}",
        )
    return RunCommandResult(
        run=detail,
        command=RunControlInfo.from_record(run, control),
    )


def _run_thread(core: AgentCore, payload: RunCreateRequest) -> str:
    if core.store.get_thread(thread_id=payload.thread) is None:
        raise HTTPException(
            status_code=404,
            detail=f"thread not found: {payload.thread}",
        )
    return payload.thread


def _run_terminal(core: AgentCore, run_id: str) -> bool:
    run = core.store.get_run(run_id=run_id)
    return run is None or run.status not in {"pending", "running"}
