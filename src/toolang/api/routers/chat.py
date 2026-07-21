"""Formal chat API routes."""

from typing import Literal

from fastapi import APIRouter, HTTPException

from toolang.api.app import ApiContext, ApiContextDep
from toolang.api.conversion import parse_user_message
from toolang.api.schemas import (
    ChatRequest,
)
from toolang.base.types.message import Message
from toolang.execution.projection import ExecutionProjector
from toolang.execution.schemas import ChatResult
from toolang.plugin.models.resolution import split_model_selectors
from toolang.plugin.tools.registry import split_tool_selectors
from toolang.state.state import split_cap_selectors
from toolang.execution.records import ThreadPeer
from toolang.execution.records import RunRecord
from toolang.execution.reply import BufferedReplySink, SseReplySink, TraceReplySink
from toolang.execution.request import RunRequest
from .._streaming import ShutdownAwareStreamingResponse, guarded_stream
from .threads import parse_thread_peer, thread_info


router = APIRouter(prefix="/chat", tags=["chat"])


@router.post("", summary="Submit Chat", response_model=ChatResult)
async def submit_chat(context: ApiContextDep, payload: ChatRequest) -> ChatResult:
    thread_id = _chat_thread_id_or_404(context, payload)
    result, reply = await _submit_chat_run(context, payload, thread_id=thread_id)
    projector = ExecutionProjector(context.executor.store)
    detail = projector.run_detail(result.run_id)
    if detail is None:
        if result.status == "failed" and result.error:
            raise HTTPException(status_code=500, detail=result.error)
        raise HTTPException(
            status_code=500,
            detail=f"run not found after completion: {result.run_id}",
        )
    if detail.input is None:
        raise HTTPException(
            status_code=500, detail=f"missing chat input for run {result.run_id}"
        )
    user_message = detail.input
    fallback_assistant = next(
        (
            item.message
            for item in reversed(detail.output.steps)
            if item.message is not None and item.message.role == "assistant"
        ),
        None,
    )
    assistant_message = (
        reply.assistant
        if reply.assistant is not None
        else fallback_assistant
        if fallback_assistant is not None
        else None
    )
    if user_message is None or assistant_message is None:
        raise HTTPException(
            status_code=500,
            detail=f"incomplete chat transcript for run {result.run_id}",
        )
    if result.thread_id is None:
        raise HTTPException(
            status_code=500, detail=f"missing chat thread for run {result.run_id}"
        )
    return ChatResult(
        thread=thread_info(context, result.thread_id),
        run=projector.run_info(result),
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
    thread_id: str | None,
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
    thread_id: str | None,
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
    thread_id: str | None,
    run_id: str,
) -> RunRequest:
    return RunRequest(
        group="chat",
        origin="chat",
        run_id=run_id,
        thread_id=thread_id,
        thread_kind=payload.client,
        message=_chat_user_message(payload),
        model_selector=payload.model,
        model_selectors=_model_selectors(payload),
        executable_kind=_executable_kind(payload),
        executable_name=_executable_name(payload),
        tool_selectors=_tool_selectors(payload),
        cap_selectors=_cap_selectors(payload),
        metadata=_thread_metadata(payload),
    )


def _chat_thread_id_or_404(context: ApiContext, payload: ChatRequest) -> str | None:
    if payload.thread is None:
        return None
    runs = context.executor.store.list_runs(thread_id=payload.thread, limit=1)
    thread = context.executor.store.get_thread(thread_id=payload.thread)
    if not runs and thread is None:
        raise HTTPException(
            status_code=404, detail=f"chat thread not found: {payload.thread}"
        )
    origin = runs[0].origin if runs else thread.origin if thread is not None else ""
    if origin != "chat":
        raise HTTPException(
            status_code=404, detail=f"chat thread not found: {payload.thread}"
        )
    peer = _request_peer(payload)
    if peer is not None:
        existing_peer = thread.peer if thread is not None else ThreadPeer()
        if existing_peer != peer:
            raise HTTPException(
                status_code=409, detail=f"chat thread peer mismatch: {payload.thread}"
            )
    return payload.thread


def _chat_user_message(payload: ChatRequest) -> Message:
    return parse_user_message(payload.message)


def _request_peer(payload: ChatRequest) -> ThreadPeer | None:
    return parse_thread_peer(payload.peer)


def _thread_metadata(payload: ChatRequest) -> dict[str, object]:
    metadata: dict[str, object] = {}
    if payload.request_id is not None:
        metadata["request_id"] = payload.request_id
    peer = _request_peer(payload)
    if peer is not None:
        metadata["thread_peer"] = peer.to_data()
    return metadata


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


def _model_selectors(payload: ChatRequest) -> tuple[str, ...]:
    return tuple(dict.fromkeys(split_model_selectors(tuple(payload.models))))


def _tool_selectors(payload: ChatRequest) -> tuple[str, ...] | None:
    if payload.tools is None:
        return None
    return tuple(dict.fromkeys(split_tool_selectors(tuple(payload.tools))))


def _cap_selectors(payload: ChatRequest) -> tuple[str, ...]:
    return tuple(dict.fromkeys(split_cap_selectors(tuple(payload.caps))))
