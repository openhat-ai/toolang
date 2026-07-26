"""Thread inspection and management routes."""

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.sse import EventSourceResponse, ServerSentEvent

from toolang.api.app import AgentCoreDep, LiveEventRelayDep
from toolang.api.common import EventSubscription, sse_stream
from toolang.api.schemas import (
    ThreadCreateRequest,
    ThreadForkRequest,
    ThreadPeerPayload,
    ThreadResult,
    ThreadRewindRequest,
)
from toolang.execution.records import ThreadPeer
from toolang.execution.schemas import ThreadDetail, ThreadInfo
from toolang.execution.types import ThreadPrefix
from toolang.up import AgentCore

router = APIRouter(prefix="/threads", tags=["threads"])


async def _subscribe_thread(
    core: AgentCoreDep,
    live: LiveEventRelayDep,
    thread_id: str,
) -> AsyncIterator[EventSubscription]:
    _thread_or_404(core, thread_id)
    subscription = live.subscribe_thread(thread_id)
    try:
        yield subscription
    finally:
        subscription.close()


@router.post(
    "",
    summary="Create Thread",
    status_code=201,
    response_model=ThreadResult,
)
def create_thread(core: AgentCoreDep, payload: ThreadCreateRequest) -> ThreadResult:
    thread_id = core.threads.create(
        prefix=_thread_prefix(payload.client),
        peer=parse_thread_peer(payload.peer),
    )
    return ThreadResult(thread=thread_info(core, thread_id))


@router.get("", summary="List Threads", response_model=list[ThreadInfo])
def threads(
    core: AgentCoreDep,
    limit: int = Query(default=50),
    origin: str | None = None,
    channel: str | None = None,
    status: str | None = None,
) -> list[ThreadInfo]:
    return core.history.list_threads(
        limit=limit,
        origin=origin,
        channel=channel,
        status=status,
    )


@router.get("/{thread_id}", summary="Get Thread", response_model=ThreadDetail)
def thread_detail(
    core: AgentCoreDep,
    thread_id: str,
    limit: int = Query(default=50),
) -> ThreadDetail:
    detail = core.history.get_thread(thread_id, run_limit=limit)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"thread not found: {thread_id}")
    return detail


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


def parse_thread_peer(payload: ThreadPeerPayload | None) -> ThreadPeer | None:
    """Parse one API peer payload into its execution value object."""

    if payload is None:
        return None
    try:
        return ThreadPeer.from_data(payload.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


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


def _thread_prefix(client: str) -> ThreadPrefix:
    if client == "web":
        return ThreadPrefix.WEB
    if client == "script":
        return ThreadPrefix.SCRIPT
    return ThreadPrefix.TERM
