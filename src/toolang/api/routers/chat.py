"""Formal chat API routes."""

from typing import Literal

from fastapi import APIRouter, HTTPException

from toolang.api.app import ApiContext, ApiContextDep
from toolang.api.conversion import parse_user_message
from toolang.api.schemas import (
    ChatRequest,
)
from toolang.base.types.message import Message
from toolang.execution.inspection import ExecutionInspection
from toolang.execution.schemas import ChatResult
from toolang.execution.records import ThreadPeer
from toolang.execution.records import RunRecord
from toolang.execution.threads import ThreadManager
from toolang.execution.reply import BufferedReplySink, SseReplySink, TraceReplySink
from toolang.execution.executor.request import RunRequest
from ..common import ShutdownAwareStreamingResponse, guarded_stream
from .threads import parse_thread_peer, thread_info


router = APIRouter(prefix="/chat", tags=["chat"])


@router.post("", summary="Submit Chat", response_model=ChatResult)
async def submit_chat(context: ApiContextDep, payload: ChatRequest) -> ChatResult:
    thread_id = _chat_thread_id_or_404(context, payload)
    result, reply = await _submit_chat_run(context, payload, thread_id=thread_id)
    inspection = ExecutionInspection(context.executor.store)
    detail = inspection.run_detail(result.id)
    if detail is None:
        if result.status == "failed" and result.error:
            raise HTTPException(status_code=500, detail=result.error)
        raise HTTPException(
            status_code=500,
            detail=f"run not found after completion: {result.id}",
        )
    if detail.input is None:
        raise HTTPException(
            status_code=500, detail=f"missing chat input for run {result.id}"
        )
    user_message = detail.input
    fallback_output = context.executor.store.run_output(run_id=result.id)
    fallback_assistant = (
        Message(role="assistant", parts=fallback_output) if fallback_output else None
    )
    assistant_message = (
        reply.assistant
        if reply.assistant is not None
        else fallback_assistant
        if fallback_assistant is not None
        else None
    )
    if assistant_message is None:
        raise HTTPException(
            status_code=500,
            detail=f"incomplete chat transcript for run {result.id}",
        )
    return ChatResult(
        thread=thread_info(context, result.thread),
        run=inspection.run_info(result),
        message=user_message,
        assistant=assistant_message,
    )


@router.post("/stream", summary="Submit Chat Stream")
async def submit_chat_stream(
    context: ApiContextDep,
    payload: ChatRequest,
) -> ShutdownAwareStreamingResponse:
    thread_id = _chat_thread_id_or_404(context, payload)
    return ShutdownAwareStreamingResponse(
        guarded_stream(_stream_chat_run(context, payload, thread_id=thread_id)),
        shutdown_signal=context.shutdown_signal,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "x-vercel-ai-ui-message-stream": "v1",
        },
    )


async def _submit_chat_run(
    context: ApiContext,
    payload: ChatRequest,
    *,
    thread_id: str,
) -> tuple[RunRecord, BufferedReplySink]:
    reply = BufferedReplySink()
    run_id = context.executor.allocate_run_id()

    record = await context.executor.run(
        _chat_run_request(payload, thread_id=thread_id, run_id=run_id),
        context.state_watcher.current(),
        reply=reply,
    )
    return record, reply


async def _stream_chat_run(
    context: ApiContext,
    payload: ChatRequest,
    *,
    thread_id: str,
):
    reply = (
        TraceReplySink()
        if payload.client == "tui"
        else SseReplySink(thread_id=thread_id)
    )
    run_id = context.executor.allocate_run_id()
    context.spawn_run(
        _chat_run_request(payload, thread_id=thread_id, run_id=run_id),
        reply=reply,
    )
    async for chunk in reply.stream():
        yield chunk


def _chat_run_request(
    payload: ChatRequest,
    *,
    thread_id: str,
    run_id: str,
) -> RunRequest:
    return RunRequest(
        origin="chat",
        input=_chat_user_message(payload),
        run_id=run_id,
        thread_id=thread_id,
        model_selector=payload.model,
        executable_kind=_executable_kind(payload),
        executable_name=_executable_name(payload),
        request_id=payload.request_id,
    )


def _chat_thread_id_or_404(context: ApiContext, payload: ChatRequest) -> str:
    if payload.thread is None:
        result = ThreadManager(context.executor).create(
            kind=payload.client,
            peer=_request_peer(payload),
        )
        return result.thread.thread_id
    thread = context.executor.store.get_thread(thread_id=payload.thread)
    if thread is None:
        raise HTTPException(
            status_code=404, detail=f"chat thread not found: {payload.thread}"
        )
    if thread.origin != "chat":
        raise HTTPException(
            status_code=404, detail=f"chat thread not found: {payload.thread}"
        )
    peer = _request_peer(payload)
    if peer is not None:
        if thread.peer != peer:
            raise HTTPException(
                status_code=409, detail=f"chat thread peer mismatch: {payload.thread}"
            )
    return payload.thread


def _chat_user_message(payload: ChatRequest) -> Message:
    return parse_user_message(payload.message)


def _request_peer(payload: ChatRequest) -> ThreadPeer | None:
    return parse_thread_peer(payload.peer)


def _executable_kind(payload: ChatRequest) -> Literal["agic", "flow"]:
    if (
        _text_or_none(payload.agic) is not None
        and _text_or_none(payload.flow) is not None
    ):
        raise HTTPException(
            status_code=422, detail="chat request cannot specify both agic and flow"
        )
    if _text_or_none(payload.flow) is not None:
        return "flow"
    if _text_or_none(payload.agic) is not None:
        return "agic"
    return "agic"


def _executable_name(payload: ChatRequest) -> str | None:
    return _text_or_none(payload.flow) or _text_or_none(payload.agic)


def _text_or_none(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None
