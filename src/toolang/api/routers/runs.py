"""Run execution, inspection, control, and live event routes."""

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.sse import EventSourceResponse, ServerSentEvent

from toolang.api.app import AgentCoreDep, LiveEventRelayDep
from toolang.api.common import EventSubscription, sse_stream
from toolang.api.conversion import parse_parts, parse_user_message
from toolang.api.schemas import (
    RunCancelRequest,
    RunCommandResult,
    RunCreateRequest,
    RunRerunRequest,
    RunRetryRequest,
    RunSteerRequest,
)
from toolang.base.types.policy import RunBindings
from toolang.common.errors import ToolangError
from toolang.execution.executor import RunHandle, RunSpec
from toolang.execution.records import RunControlRecord, RunRecord
from toolang.execution.schemas import ControlInfo, RunDetail, RunInfo
from toolang.execution.types import RunStatus
from toolang.lang.input import resolve_runnable_input
from toolang.execution.runnables import parse_runnable_ref, resolve_runnable
from toolang.up import AgentCore

router = APIRouter(prefix="/runs", tags=["runs"])
_StartedRunStream = tuple[RunHandle, EventSubscription]


async def _start_run_stream(
    core: AgentCoreDep,
    live: LiveEventRelayDep,
    payload: RunCreateRequest,
) -> AsyncIterator[_StartedRunStream]:
    thread_id = _run_thread(core, payload)
    setup = core.setup.current()
    limits = (
        payload.limits.to_limits(setup.limits)
        if payload.limits is not None
        else setup.limits
    )
    try:
        state = core.state.current()
        runnable_name, runnable_kind = parse_runnable_ref(payload.runnable)
        runnable = resolve_runnable(
            state.program,
            runnable_name,
            kind=runnable_kind,
        )
        handle = core.executor.start(
            RunSpec(
                setup=setup,
                state=state,
                thread=thread_id,
                bindings=RunBindings(
                    runnable=payload.runnable,
                    model=(
                        payload.model
                        if payload.model is not None
                        else setup.bindings.model
                    ),
                ),
                limits=limits,
                space=payload.space,
                input=resolve_runnable_input(
                    runnable,
                    primary=parse_parts(payload.input) if payload.input else None,
                    named=payload.args,
                    structs={item.name: item for item in state.program.structs},
                ),
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
                f"subscribe to {core.store.root_run_id(run_id=run_id)}"
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


@router.post(
    "/{run_id}/retry",
    summary="Retry Run",
    status_code=202,
    response_model=RunCommandResult,
)
async def retry_run(
    core: AgentCoreDep,
    live: LiveEventRelayDep,
    run_id: str,
    payload: RunRetryRequest | None = None,
) -> RunCommandResult:
    source = _terminal_root_or_409(core, run_id)
    request = payload or RunRetryRequest()
    setup = core.setup.current()
    try:
        handle = core.executor.retry(
            source.id,
            setup=setup,
            state=core.state.current(),
            anchor=request.anchor,
            model=(
                request.model if request.model is not None else setup.bindings.model
            ),
            limits=(
                request.limits.to_limits(setup.limits)
                if request.limits is not None
                else None
            ),
            request_id=request.request_id,
            tracer=live.trace(thread_id=source.thread),
        )
    except (ToolangError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    control = core.store.list_run_controls(run_id=handle.run_id)[-1]
    return _control_result(core, handle.run_id, control)


@router.post(
    "/{run_id}/rerun",
    summary="Rerun Run",
    status_code=202,
    response_model=RunCommandResult,
)
async def rerun_run(
    core: AgentCoreDep,
    live: LiveEventRelayDep,
    run_id: str,
    payload: RunRerunRequest | None = None,
) -> RunCommandResult:
    source = _terminal_root_or_409(core, run_id)
    request = payload or RunRerunRequest()
    setup = core.setup.current()
    try:
        handle = core.executor.rerun(
            source.id,
            setup=setup,
            state=core.state.current(),
            model=(
                request.model if request.model is not None else setup.bindings.model
            ),
            limits=(
                request.limits.to_limits(setup.limits)
                if request.limits is not None
                else None
            ),
            request_id=request.request_id,
            tracer=live.trace(thread_id=source.thread),
        )
    except (ToolangError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    control = core.store.get_run_control(run_id=handle.run_id, index=0)
    if control is None:  # pragma: no cover - executor acceptance is atomic
        raise HTTPException(status_code=500, detail="rerun control not found")
    return _control_result(core, handle.run_id, control)


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


def _terminal_root_or_409(core: AgentCore, run_id: str) -> RunRecord:
    run = _run_or_404(core, run_id)
    if run.parent is not None:
        raise HTTPException(status_code=409, detail=f"run is not a root: {run_id}")
    if run.status in {"pending", "running"}:
        raise HTTPException(status_code=409, detail=f"run is not terminal: {run_id}")
    return run


def _control_result(
    core: AgentCore,
    run_id: str,
    control: RunControlRecord,
) -> RunCommandResult:
    run = _run_or_404(core, run_id)
    detail = core.history.get_run(run_id)
    if detail is None:
        raise HTTPException(
            status_code=500,
            detail=f"run not found after control: {run_id}",
        )
    return RunCommandResult(
        run=detail,
        command=ControlInfo.from_record(run, control),
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
