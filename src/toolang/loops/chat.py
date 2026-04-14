"""Formal chat API routes."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ..execution.detail import run_detail_from_record
from ..execution.response import BufferedResponseSink, SseResponseSink
from ..execution.runner import RunRequest
from ..execution.runner import RunOutcome

if TYPE_CHECKING:
    from ..up import UptimeContext


class ChatRequest(BaseModel):
    """One formal chat submission."""

    thread: str = Field(min_length=1)
    message: str = Field(min_length=1)
    thunk: str | None = None
    model: str | None = None


def create_router() -> APIRouter:
    """Build the formal chat route group."""

    router = APIRouter(prefix="/api/v1", tags=["chat"])

    @router.post("/chat", summary="Submit Chat")
    async def submit_chat(request: Request, payload: ChatRequest) -> dict[str, object]:
        context = request.app.state.runtime
        result, response = await _submit_chat_run(context, payload)
        run = context.store.get_run(run_id=result.run_id)
        if run is None:
            raise HTTPException(status_code=500, detail=f"run not found after completion: {result.run_id}")
        detail = run_detail_from_record(
            run,
            steps=context.store.list_steps(run_id=result.run_id),
        )
        if detail.input is None:
            raise HTTPException(status_code=500, detail=f"missing chat input for run {result.run_id}")
        user_message = asdict(detail.input)
        fallback_assistant = next(
            (
                item.message
                for item in reversed(detail.output.steps)
                if item.message is not None and item.message.role == "assistant"
            ),
            None,
        )
        assistant_message = (
            asdict(response.assistant)
            if response.assistant is not None
            else asdict(fallback_assistant) if fallback_assistant is not None else None
        )
        if user_message is None or assistant_message is None:
            raise HTTPException(status_code=500, detail=f"incomplete chat transcript for run {result.run_id}")
        return {
            "thread_id": payload.thread,
            "run_id": result.run_id,
            "message": user_message,
            "assistant": assistant_message,
        }

    @router.post("/chat/stream", summary="Submit Chat Stream")
    async def submit_chat_stream(request: Request, payload: ChatRequest) -> StreamingResponse:
        context = request.app.state.runtime
        return StreamingResponse(
            _stream_chat_run(context, payload),
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
) -> tuple[RunOutcome, BufferedResponseSink]:
    response = BufferedResponseSink()
    loop = asyncio.get_running_loop()
    completion: asyncio.Future[RunOutcome] = loop.create_future()

    context.runner.enqueue(
        RunRequest(
            group="chat",
            origin="chat",
            thread_id=payload.thread,
            thunk=payload.message,
            thunk_name=payload.thunk,
            metadata={"model": payload.model} if payload.model else {},
        ),
        response=response,
        completion=completion,
    )
    return await completion, response


async def _stream_chat_run(context: UptimeContext, payload: ChatRequest):
    response = SseResponseSink(thread_id=payload.thread)
    context.runner.enqueue(
        RunRequest(
            group="chat",
            origin="chat",
            thread_id=payload.thread,
            thunk=payload.message,
            thunk_name=payload.thunk,
            metadata={"model": payload.model} if payload.model else {},
        ),
        response=response,
    )
    async for chunk in response.stream():
        yield chunk
