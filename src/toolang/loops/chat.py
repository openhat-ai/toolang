"""Formal chat API routes."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from toolang.base.error import ToolangError
from toolang.base.types.message import Message
from ..execution.detail import run_detail_from_record
from ..execution.input import effective_origin_model_selectors
from ..execution.model import resolve_model
from ..execution.response import BufferedResponseSink, SseResponseSink
from ..execution.runner import RunRequest
from ..execution.runner import RunOutcome

if TYPE_CHECKING:
    from ..up import UptimeContext


class ChatMessagePayload(BaseModel):
    """One structured chat message payload."""

    role: str = Field(default="user", min_length=1)
    parts: list[dict[str, object]] = Field(min_length=1)
    meta: dict[str, object] = Field(default_factory=dict)


class ChatRequest(BaseModel):
    """One formal chat submission."""

    thread: str = Field(min_length=1)
    message: ChatMessagePayload
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
        return {
            "thread_id": payload.thread,
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
    user_message = _chat_user_message(payload)

    context.runner.enqueue(
        RunRequest(
            group="chat",
            origin="chat",
            thread_id=payload.thread,
            message=user_message,
            model_selector=payload.model,
        ),
        response=response,
        completion=completion,
    )
    return await completion, response


async def _stream_chat_run(context: UptimeContext, payload: ChatRequest):
    response = SseResponseSink(thread_id=payload.thread)
    user_message = _chat_user_message(payload)
    context.runner.enqueue(
        RunRequest(
            group="chat",
            origin="chat",
            thread_id=payload.thread,
            message=user_message,
            model_selector=payload.model,
        ),
        response=response,
    )
    async for chunk in response.stream():
        yield chunk


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
