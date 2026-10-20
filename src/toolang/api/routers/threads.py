"""Thread inspection and management routes."""

from collections.abc import AsyncIterator
from dataclasses import replace
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.sse import EventSourceResponse, ServerSentEvent

from toolang.api.app import AgentCoreDep, require_chat_driver
from toolang.api.common import StreamAttachmentDep, sse_stream
from toolang.execution.subscriptions import EventSubscription, StreamScope
from toolang.api.schemas import (
    ThreadCreateRequest,
    ThreadForkRequest,
    ThreadResult,
    ThreadRewindRequest,
)
from toolang.execution.schemas import RunDetail, ThreadDetail, ThreadInfo
from toolang.execution.types import ThreadKind
from toolang.execution.values import parts_from_value
from toolang.up import AgentCore

router = APIRouter(prefix="/threads", tags=["threads"])


async def _subscribe_thread(
    core: AgentCoreDep,
    attachment: StreamAttachmentDep,
    thread_id: str,
) -> AsyncIterator[EventSubscription]:
    _thread_or_404(core, thread_id)
    subscription = attachment.attach(StreamScope(thread=thread_id))
    try:
        yield subscription
    finally:
        subscription.close()


@router.post(
    "",
    summary="Create Thread",
    dependencies=[Depends(require_chat_driver)],
    status_code=201,
    response_model=ThreadResult,
)
def create_thread(core: AgentCoreDep, payload: ThreadCreateRequest) -> ThreadResult:
    thread_id = core.threads.create(
        kind=ThreadKind.CHAT,
        request_id=payload.request_id,
        client_id=payload.client_id,
    )
    return ThreadResult(thread=thread_info(core, thread_id))


@router.get("", summary="List Threads", response_model=list[ThreadInfo])
def threads(
    core: AgentCoreDep,
    limit: int = Query(default=50),
    kind: ThreadKind | None = None,
    status: str | None = None,
) -> list[ThreadInfo]:
    return core.history.list_threads(
        limit=limit,
        kind=kind,
        status=status,
    )


@router.get("/{thread_id}", summary="Get Thread", response_model=ThreadDetail)
def thread_detail(
    core: AgentCoreDep,
    thread_id: str,
    limit: int = Query(default=50),
) -> ThreadDetail:
    with core.store.read_transaction():
        detail = core.history.get_thread(thread_id, run_limit=limit)
        if detail is None:
            raise HTTPException(
                status_code=404, detail=f"thread not found: {thread_id}"
            )
        return replace(
            detail,
            runs=[
                replace(run, output=core.history.get_output(run.id))
                for run in detail.runs
            ],
        )


@router.get(
    "/{thread_id}/result",
    summary="Get Latest Thread Result",
    response_model=RunDetail,
)
def latest_thread_result(core: AgentCoreDep, thread_id: str) -> RunDetail:
    with core.store.read_transaction():
        try:
            view = core.history.thread_view(thread_id)
        except KeyError:
            raise HTTPException(
                status_code=404,
                detail=f"thread not found: {thread_id}",
            ) from None
        for run in view.runs(reverse=True):
            if run.status != "succeeded" or run.output is None:
                continue
            output = core.history.get_output(run.id)
            if output is not None and parts_from_value(output.value):
                detail = core.history.get_run(run.id)
                if detail is not None:
                    return replace(detail, output=output)
    raise HTTPException(
        status_code=404,
        detail=f"thread has no result: {thread_id}",
    )


@router.post(
    "/{thread_id}/rewind",
    summary="Rewind Thread",
    response_model=ThreadResult,
)
def rewind_thread(
    core: AgentCoreDep,
    thread_id: str,
    payload: ThreadRewindRequest,
) -> ThreadResult:
    _thread_or_404(core, thread_id)
    try:
        core.threads.rewind(
            thread_id=thread_id,
            run_id=payload.run_id,
            request_id=payload.request_id,
            client_id=payload.client_id,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return ThreadResult(thread=thread_info(core, thread_id))


@router.post("/{thread_id}/fork", summary="Fork Thread", response_model=ThreadResult)
def fork_thread(
    core: AgentCoreDep,
    thread_id: str,
    payload: ThreadForkRequest,
) -> ThreadResult:
    _thread_or_404(core, thread_id)
    try:
        forked_id = core.threads.fork(
            thread_id=thread_id,
            run_id=payload.run_id,
            request_id=payload.request_id,
            client_id=payload.client_id,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return ThreadResult(thread=thread_info(core, forked_id))


@router.get(
    "/{thread_id}/stream",
    summary="Stream Thread Events",
    response_class=EventSourceResponse,
)
async def thread_stream(
    request: Request,
    thread_id: str,
    subscription: Annotated[EventSubscription, Depends(_subscribe_thread)],
) -> AsyncIterator[ServerSentEvent]:
    async for event in sse_stream(request, subscription):
        yield event


def thread_info(core: AgentCore, thread_id: str) -> ThreadInfo:
    """Return one persisted thread after a write operation."""

    info = core.history.get_thread(thread_id, run_limit=0)
    if info is None:
        raise HTTPException(
            status_code=500,
            detail=f"thread not found after completion: {thread_id}",
        )
    return info


def _thread_or_404(core: AgentCore, thread_id: str) -> None:
    if core.store.get_thread(thread_id=thread_id) is not None:
        return
    raise HTTPException(status_code=404, detail=f"thread not found: {thread_id}")
