"""Thread inspection and management routes."""

from fastapi import APIRouter, HTTPException, Query, Request

from toolang.api.app import ApiContext, ApiContextDep
from toolang.api.conversion import parse_user_message
from toolang.api.schemas import (
    ThreadCreateRequest,
    ThreadForkRequest,
    ThreadPeerPayload,
    ThreadRewindRequest,
)
from toolang.base.types.message import Message
from toolang.execution.inspection import ExecutionInspection
from toolang.execution.records import ThreadPeer
from toolang.execution.executor.request import RunRequest
from toolang.execution.schemas import (
    RunCommandResult,
    RunControlInfo,
    ThreadDetail,
    ThreadInfo,
    ThreadResult,
)
from toolang.execution.stream import event_data, stream_events
from toolang.execution.thread import ThreadChange, ThreadOperations
from ..common import ShutdownAwareStreamingResponse, event_stream_response


router = APIRouter(prefix="/threads", tags=["threads"])


@router.post(
    "", summary="Create Chat Thread", status_code=201, response_model=ThreadResult
)
def create_thread(context: ApiContextDep, payload: ThreadCreateRequest) -> ThreadResult:
    thread = ThreadOperations(context.executor).create(
        kind=payload.client,
        peer=parse_thread_peer(payload.peer),
    )
    return ThreadResult(thread=thread_info(context, thread.thread_id))


@router.get("", summary="List Threads", response_model=list[ThreadInfo])
def threads(
    context: ApiContextDep,
    limit: int = Query(default=50),
    origin: str | None = None,
    channel: str | None = None,
    status: str | None = None,
) -> list[ThreadInfo]:
    items = ExecutionInspection(context.executor.store).list_threads(
        limit=limit,
        origin=origin,
        channel=channel,
        status=status,
    )
    return items


@router.get("/{thread_id}", summary="Get Thread", response_model=ThreadDetail)
def thread_detail(
    context: ApiContextDep,
    thread_id: str,
    limit: int = Query(default=50),
) -> ThreadDetail:
    detail = ExecutionInspection(context.executor.store).thread_detail(
        thread_id, limit=limit
    )
    if detail is None:
        raise HTTPException(status_code=404, detail=f"thread not found: {thread_id}")
    return detail


@router.post(
    "/{thread_id}/rewind", summary="Rewind Thread", response_model=ThreadResult
)
async def rewind_thread(
    context: ApiContextDep, thread_id: str, payload: ThreadRewindRequest
) -> ThreadResult:
    _thread_anchor_or_404(context, thread_id=thread_id, run_id=payload.run_id)
    message = (
        parse_user_message(payload.message) if payload.message is not None else None
    )
    try:
        result = await ThreadOperations(context.executor).rewind(
            run_id=payload.run_id,
            message=message,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return await _thread_result(
        context,
        change=result,
        message=message,
        request_id=payload.request_id,
    )


@router.post("/{thread_id}/fork", summary="Fork Thread", response_model=ThreadResult)
async def fork_thread(
    context: ApiContextDep, thread_id: str, payload: ThreadForkRequest
) -> ThreadResult:
    _thread_anchor_or_404(context, thread_id=thread_id, run_id=payload.run_id)
    message = (
        parse_user_message(payload.message) if payload.message is not None else None
    )
    try:
        result = ThreadOperations(context.executor).fork(
            run_id=payload.run_id,
            message=message,
            include_anchor=payload.include_anchor,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return await _thread_result(
        context,
        change=result,
        message=message,
        request_id=payload.request_id,
    )


@router.get("/{thread_id}/events", summary="List Thread Events")
def thread_events(
    context: ApiContextDep,
    thread_id: str,
    after: int | None = None,
    limit: int = Query(default=100),
) -> dict[str, object]:
    _thread_or_404(context, thread_id)
    events = context.executor.store.list_events(
        domain="thread", domain_id=thread_id, after=after, limit=limit
    )
    return {
        "cursor": context.executor.store.latest_event_cursor(
            domain="thread", domain_id=thread_id
        ),
        "items": [event_data(item) for item in events],
    }


@router.get("/{thread_id}/stream", summary="Stream Thread Events")
async def thread_stream(
    context: ApiContextDep,
    request: Request,
    thread_id: str,
    after: int | None = None,
) -> ShutdownAwareStreamingResponse:
    _thread_or_404(context, thread_id)
    return event_stream_response(
        request,
        stream_events(
            context.executor.store,
            domain="thread",
            domain_id=thread_id,
            after=after,
        ),
    )


def parse_thread_peer(payload: ThreadPeerPayload | None) -> ThreadPeer | None:
    """Parse one API peer payload into its execution value object."""

    if payload is None:
        return None
    try:
        return ThreadPeer.from_data(payload.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


async def _thread_result(
    context: ApiContext,
    *,
    change: ThreadChange,
    message: Message | None,
    request_id: str | None,
) -> ThreadResult:
    if change.run_id is None or message is None:
        return ThreadResult(thread=thread_info(context, change.thread_id))
    run, command = await context.submit_run(
        RunRequest(
            origin="chat",
            input=message,
            run_id=change.run_id,
            thread_id=change.thread_id,
            request_id=request_id,
        )
    )
    inspection = ExecutionInspection(context.executor.store)
    return ThreadResult(
        thread=thread_info(context, change.thread_id),
        run=RunCommandResult(
            run=inspection.run_info(run),
            command=RunControlInfo.from_record(run, command),
        ),
    )


def thread_info(context: ApiContext, thread_id: str) -> ThreadInfo:
    """Return one persisted thread after a write operation."""

    info = ExecutionInspection(context.executor.store).thread_info(thread_id)
    if info is None:
        raise HTTPException(
            status_code=500,
            detail=f"thread not found after completion: {thread_id}",
        )
    return info


def _thread_or_404(context: ApiContext, thread_id: str) -> None:
    if context.executor.store.get_thread(thread_id=thread_id) is not None:
        return
    raise HTTPException(status_code=404, detail=f"thread not found: {thread_id}")


def _thread_anchor_or_404(context: ApiContext, *, thread_id: str, run_id: str) -> None:
    _thread_or_404(context, thread_id)
    run = context.executor.store.get_run(run_id=run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"run not found: {run_id}")
    if run.thread != thread_id:
        raise HTTPException(
            status_code=409,
            detail=f"run {run_id} does not belong to thread {thread_id}",
        )
