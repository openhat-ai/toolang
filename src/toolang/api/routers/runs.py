"""Formal execution inspection routes."""

from typing import cast

from fastapi import APIRouter, HTTPException, Query, Request

from toolang.api.app import ApiContextDep
from toolang.api.conversion import parse_user_message
from toolang.api.schemas import (
    RunCancelRequest,
    RunCreateRequest,
    RunSteerRequest,
)
from toolang.base.types.message import Message
from toolang.execution.inspection import ExecutionInspection
from toolang.execution.records import RunRecord
from toolang.execution.reply import TraceReplySink
from toolang.execution.executor.request import RunRequest
from toolang.execution.schemas import (
    RunCommandResult,
    RunControlInfo,
    RunDetail,
    RunInfo,
)
from toolang.execution.types import CommandApply, RunStatus
from toolang.execution.stream import event_data, stream_events
from ..common import (
    ShutdownAwareStreamingResponse,
    event_stream_response,
    guarded_stream,
)


router = APIRouter(prefix="/runs", tags=["runs"])


@router.get("", summary="List Runs", response_model=list[RunInfo])
def runs(
    context: ApiContextDep,
    limit: int = Query(default=50),
    thread_id: str | None = None,
    status: RunStatus | None = None,
) -> list[RunInfo]:
    items = ExecutionInspection(context.executor.store).list_runs(
        limit=limit, thread_id=thread_id, status=status
    )
    return items


@router.post("/stream", summary="Execute Run Stream")
async def execute_run_stream(
    context: ApiContextDep,
    payload: RunCreateRequest,
) -> ShutdownAwareStreamingResponse:
    reply = TraceReplySink()
    context.spawn_run(
        RunRequest(
            origin="script",
            input=Message.user(payload.input),
            run_id=context.executor.allocate_run_id(),
            executable_kind=payload.executable_kind,
            executable_name=payload.executable_name,
            model_selector=payload.model,
            context=dict(payload.metadata),
        ),
        reply=reply,
    )
    return ShutdownAwareStreamingResponse(
        guarded_stream(reply.stream()),
        shutdown_signal=getattr(context, "shutdown_signal", None),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/{run_id}", summary="Get Run", response_model=RunDetail)
def run_detail(context: ApiContextDep, run_id: str) -> RunDetail:
    detail = ExecutionInspection(context.executor.store).run_detail(run_id)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"run not found: {run_id}")
    return detail


@router.get("/{run_id}/events", summary="List Run Events")
def run_events(
    context: ApiContextDep,
    run_id: str,
    after: int | None = None,
    limit: int = Query(default=100),
) -> dict[str, object]:
    _run_or_404(context, run_id)
    events = context.executor.store.list_events(
        domain="run", domain_id=run_id, after=after, limit=limit
    )
    return {
        "cursor": context.executor.store.latest_event_cursor(
            domain="run", domain_id=run_id
        ),
        "items": [event_data(item) for item in events],
    }


@router.get("/{run_id}/stream", summary="Stream Run Events")
async def run_stream(
    context: ApiContextDep,
    request: Request,
    run_id: str,
    after: int | None = None,
) -> ShutdownAwareStreamingResponse:
    _run_or_404(context, run_id)
    return event_stream_response(
        request,
        stream_events(
            context.executor.store, domain="run", domain_id=run_id, after=after
        ),
    )


@router.post("/{run_id}/cancel", summary="Cancel Run", response_model=RunCommandResult)
async def cancel_run(
    context: ApiContextDep,
    run_id: str,
    payload: RunCancelRequest | None = None,
) -> RunCommandResult:
    run = _run_or_404(context, run_id)
    if run.status != "running":
        raise HTTPException(status_code=409, detail=f"run is not running: {run_id}")
    command_record, run = await context.executor.stop(
        run_id=run.id,
        apply=_input_apply(payload.mode if payload else "immediate"),
        request_id=payload.request_id if payload else None,
        reason=payload.reason if payload else None,
    )
    inspection = ExecutionInspection(context.executor.store)
    return RunCommandResult(
        run=inspection.run_info(run),
        command=RunControlInfo.from_record(run, command_record),
    )


@router.post(
    "/{run_id}/steer",
    summary="Steer Run",
    status_code=202,
    response_model=RunCommandResult,
)
def steer_run(
    context: ApiContextDep, run_id: str, payload: RunSteerRequest
) -> RunCommandResult:
    run = _run_or_404(context, run_id)
    if run.status != "running":
        raise HTTPException(status_code=409, detail=f"run is not running: {run_id}")
    message = parse_user_message(payload.message)
    command_record = context.executor.steer(
        run_id=run.id,
        apply=_input_apply(payload.mode),
        request_id=payload.request_id,
        message=message,
    )
    updated = _run_or_404(context, run_id)
    inspection = ExecutionInspection(context.executor.store)
    return RunCommandResult(
        run=inspection.run_info(updated),
        command=RunControlInfo.from_record(updated, command_record),
    )


def _run_or_404(context, run_id: str) -> RunRecord:
    run = context.executor.store.get_run(run_id=run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"run not found: {run_id}")
    return run


def _input_apply(value: str) -> CommandApply:
    if value not in {"immediate", "next_step", "next_call"}:
        raise HTTPException(
            status_code=422, detail=f"unsupported run input mode: {value}"
        )
    return "now" if value == "immediate" else cast(CommandApply, value)
