"""Formal chat API routes."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any, cast

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from toolang.base.error import ToolangError
from toolang.base.types.message import Message
from ..execution.detail import run_detail_from_record, thread_info_from_record, thread_info_from_runs
from ..execution.input import effective_origin_model_selectors
from ..execution.model import resolve_model
from ..execution.records import ThreadPeer
from ..execution.response import BufferedResponseSink, SseResponseSink
from ..execution.runner import RunRequest
from ..execution.runner import RunOutcome
from .streaming import ShutdownAwareStreamingResponse

if TYPE_CHECKING:
    from ..up import UptimeContext


class ChatMessagePayload(BaseModel):
    """One structured chat message payload."""

    role: str = Field(default="user", min_length=1)
    parts: list[dict[str, object]] = Field(min_length=1)
    meta: dict[str, object] = Field(default_factory=dict)


class ThreadPeerPayload(BaseModel):
    """One optional chat thread peer descriptor."""

    type: str = Field(default="user", min_length=1)
    name: str = Field(default="user", min_length=1)
    thread: str | None = None


class ChatRequest(BaseModel):
    """One formal chat submission."""

    thread: str | None = Field(default=None, min_length=1)
    peer: ThreadPeerPayload | None = None
    request_id: str | None = Field(default=None, min_length=1)
    message: ChatMessagePayload
    model: str | None = None


def create_router() -> APIRouter:
    """Build the formal chat route group."""

    router = APIRouter(prefix="/api/v1", tags=["chat"])

    @router.post("/chat", summary="Submit Chat")
    async def submit_chat(request: Request, payload: ChatRequest) -> dict[str, object]:
        context = request.app.state.runtime
        thread_id = _chat_thread_id_or_404(context, payload)
        result, response = await _submit_chat_run(context, payload, thread_id=thread_id)
        run = context.store.get_run(run_id=result.run_id)
        if run is None:
            raise HTTPException(status_code=500, detail=f"run not found after completion: {result.run_id}")
        detail = run_detail_from_record(
            run,
            steps=context.store.list_steps(run_id=result.run_id),
            inputs=context.store.list_inputs(run_id=result.run_id),
        )
        if detail.input is None:
            raise HTTPException(status_code=500, detail=f"missing chat input for run {result.run_id}")
        user_message = detail.input.to_data()
        fallback_assistant = next(
            (
                item.message
                for item in reversed(detail.output.steps)
                if item.message is not None and item.message.role == "assistant"
            ),
            None,
        )
        assistant_message = (
            response.assistant.to_data()
            if response.assistant is not None
            else fallback_assistant.to_data() if fallback_assistant is not None else None
        )
        if user_message is None or assistant_message is None:
            raise HTTPException(status_code=500, detail=f"incomplete chat transcript for run {result.run_id}")
        if result.thread_id is None:
            raise HTTPException(status_code=500, detail=f"missing chat thread for run {result.run_id}")
        return {
            "thread_id": result.thread_id,
            "thread": asdict(_thread_info(context, result.thread_id)),
            "run_id": result.run_id,
            "message": user_message,
            "assistant": assistant_message,
        }

    @router.get("/chat/models", summary="List Chat Models")
    async def chat_models(request: Request) -> dict[str, object]:
        context = request.app.state.runtime
        try:
            selectors = effective_origin_model_selectors(context, origin="chat")
        except ToolangError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        items = [
            _chat_model_item(
                selector=selector,
                context=context,
            )
            for selector in selectors
        ]
        return {
            "default": selectors[0] if selectors else None,
            "items": items,
        }

    @router.post("/chat/stream", summary="Submit Chat Stream")
    async def submit_chat_stream(request: Request, payload: ChatRequest) -> ShutdownAwareStreamingResponse:
        context = request.app.state.runtime
        thread_id = _chat_thread_id_or_404(context, payload)
        return ShutdownAwareStreamingResponse(
            _guarded_stream(_stream_chat_run(context, payload, thread_id=thread_id)),
            shutdown_signal=getattr(request.app.state, "shutdown_signal", None),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
                "x-vercel-ai-ui-message-stream": "v1",
            },
        )

    return router


async def _submit_chat_run(
    context: UptimeContext,
    payload: ChatRequest,
    *,
    thread_id: str | None,
) -> tuple[RunOutcome, BufferedResponseSink]:
    response = BufferedResponseSink()
    loop = asyncio.get_running_loop()
    completion: asyncio.Future[RunOutcome] = loop.create_future()
    user_message = _chat_user_message(payload)

    context.runner.enqueue(
        RunRequest(
            group="chat",
            origin="chat",
            thread_id=thread_id,
            message=user_message,
            model_selector=payload.model,
            metadata=_thread_metadata(payload),
        ),
        response=response,
        completion=completion,
    )
    return await completion, response


async def _stream_chat_run(
    context: UptimeContext,
    payload: ChatRequest,
    *,
    thread_id: str | None,
):
    response = SseResponseSink(thread_id=thread_id)
    user_message = _chat_user_message(payload)
    context.runner.enqueue(
        RunRequest(
            group="chat",
            origin="chat",
            thread_id=thread_id,
            message=user_message,
            model_selector=payload.model,
            metadata=_thread_metadata(payload),
        ),
        response=response,
    )
    async for chunk in response.stream():
        yield chunk


async def _guarded_stream(
    stream: AsyncIterator[str],
) -> AsyncIterator[str]:
    try:
        async for chunk in stream:
            yield chunk
    except asyncio.CancelledError:
        return
    finally:
        aclose = getattr(stream, "aclose", None)
        if callable(aclose):
            await cast(Any, aclose)()


def _chat_thread_id_or_404(context: UptimeContext, payload: ChatRequest) -> str | None:
    if payload.thread is None:
        return None
    runs = context.store.list_runs(thread_id=payload.thread, limit=1)
    thread = context.store.get_thread(thread_id=payload.thread)
    if not runs and thread is None:
        raise HTTPException(status_code=404, detail=f"chat thread not found: {payload.thread}")
    origin = runs[0].origin if runs else thread.origin if thread is not None else ""
    if origin != "chat":
        raise HTTPException(status_code=404, detail=f"chat thread not found: {payload.thread}")
    peer = _request_peer(payload)
    if peer is not None:
        existing_peer = thread.peer if thread is not None else ThreadPeer()
        if existing_peer != peer:
            raise HTTPException(status_code=409, detail=f"chat thread peer mismatch: {payload.thread}")
    return payload.thread


def _chat_user_message(payload: ChatRequest) -> Message:
    try:
        message = Message.from_data(payload.message.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if message.role != "user":
        raise HTTPException(status_code=422, detail="chat message role must be user")
    if not message.parts:
        raise HTTPException(status_code=422, detail="chat message parts cannot be empty")
    return message


def _request_peer(payload: ChatRequest) -> ThreadPeer | None:
    if payload.peer is None:
        return None
    try:
        return ThreadPeer.from_data(payload.peer.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _thread_metadata(payload: ChatRequest) -> dict[str, object]:
    metadata: dict[str, object] = {}
    if payload.request_id is not None:
        metadata["request_id"] = payload.request_id
    peer = _request_peer(payload)
    if peer is not None:
        metadata["thread_peer"] = peer.to_data()
    return metadata


def _thread_info(context: UptimeContext, thread_id: str):
    runs = sorted(
        context.store.list_runs(limit=None, thread_id=thread_id),
        key=lambda item: item.created_at,
    )
    thread = context.store.get_thread(thread_id=thread_id)
    if not runs:
        if thread is None:
            raise HTTPException(status_code=500, detail=f"thread not found after completion: {thread_id}")
        return thread_info_from_record(thread)
    steps_by_run = context.store.list_steps_for_runs(run_ids=tuple(run.run_id for run in runs))
    inputs_by_run = {run.run_id: context.store.list_inputs(run_id=run.run_id) for run in runs}
    return thread_info_from_runs(
        thread_id,
        runs,
        inputs_by_run=inputs_by_run,
        steps_by_run=steps_by_run,
        thread=thread,
    )


def _chat_model_item(*, selector: str, context: UptimeContext) -> dict[str, object]:
    target = resolve_model(
        context,
        selector=selector,
    )
    return {
        "selector": selector,
        "name": target.name,
        "ref": target.ref,
        "provider": target.provider,
        "model": target.model,
        "adapter": target.adapter,
        "tools": target.tools,
        "streaming": target.streaming,
    }
