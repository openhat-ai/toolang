"""Formal chat API routes."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any, Literal, cast

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from toolang.common.error import ToolangError
from toolang.base.types.message import Message
from toolang.execution.detail import run_detail_from_record, thread_info_from_record, thread_info_from_runs
from toolang.execution.binding import allocate_run_id, allocate_thread_id
from toolang.execution.effective import effective_origin_model_selectors, select_origin_agic
from toolang.plugin.models.resolution import selectable_model_targets, split_model_selectors
from toolang.plugin.tools.registry import split_tool_selectors
from toolang.catalog.cap import split_cap_selectors
from toolang.execution.records import ThreadPeer
from toolang.execution.records import RunRecord
from toolang.execution.reply import BufferedReplySink, SseReplySink, TraceReplySink
from toolang.execution.request import RunRequest
from ._streaming import ShutdownAwareStreamingResponse

if TYPE_CHECKING:
    from toolang.agent.context import ComponentState


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
    client: Literal["web", "term", "tui", "chat"] = "web"
    peer: ThreadPeerPayload | None = None
    request_id: str | None = Field(default=None, min_length=1)
    message: ChatMessagePayload
    model: str | None = None
    models: list[str] = Field(default_factory=list)
    agic: str | None = None
    flow: str | None = None
    tools: list[str] | None = None
    caps: list[str] = Field(default_factory=list)


class ThreadCreateRequest(BaseModel):
    """Request one empty chat thread."""

    client: Literal["web", "term", "tui", "chat"] = "term"
    peer: ThreadPeerPayload | None = None


def create_router() -> APIRouter:
    """Build the formal chat route group."""

    router = APIRouter(prefix="/api/v1", tags=["chat"])

    @router.post("/threads", summary="Create Chat Thread")
    async def create_thread(request: Request, payload: ThreadCreateRequest) -> dict[str, object]:
        context = request.app.state
        thread_id = _new_thread_id(context, payload.client)
        peer = _peer_payload(payload.peer)
        context.store.ensure_thread(
            thread_id=thread_id,
            origin="chat",
            peer=peer,
        )
        return {
            "thread_id": thread_id,
            "thread": asdict(_thread_info(context, thread_id)),
        }

    @router.post("/chat", summary="Submit Chat")
    async def submit_chat(request: Request, payload: ChatRequest) -> dict[str, object]:
        context = request.app.state
        thread_id = _chat_thread_id_or_404(context, payload)
        result, reply = await _submit_chat_run(context, payload, thread_id=thread_id)
        run = context.store.get_run(run_id=result.run_id)
        if run is None:
            if result.status == "failed" and result.error:
                raise HTTPException(status_code=500, detail=result.error)
            raise HTTPException(status_code=500, detail=f"run not found after completion: {result.run_id}")
        detail = run_detail_from_record(
            run,
            steps=context.store.list_steps(run_id=result.run_id),
            inputs=context.store.list_commands(run_id=result.run_id),
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
            reply.assistant.to_data()
            if reply.assistant is not None
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
        context = request.app.state
        try:
            selectors = effective_origin_model_selectors(
                context.executor,
                state=context.get_agent_state(),
                origin="chat",
            )
            targets = selectable_model_targets(
                providers=context.executor.setup.model_providers,
                aliases=context.executor.model_aliases,
                environ=context.executor.model_environ,
                selectors=selectors,
                cache_dir=context.executor.model_cache_dir,
                refresh=context.executor.model_cache_refresh,
            )
        except ToolangError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        items = [
            _chat_model_item(
                selector=selector,
                target=target,
            )
            for selector, target in targets
        ]
        return {
            "default": selectors[0] if selectors else None,
            "items": items,
        }

    @router.get("/chat/agics", summary="List Chat Agics")
    async def chat_agics(request: Request) -> dict[str, object]:
        program = request.app.state.get_agent_state().program
        return {
            "default": _default_agic_name(program, origin="chat"),
            "items": [{"name": agic.name} for agic in program.available_agics],
        }

    @router.get("/chat/flows", summary="List Chat Flows")
    async def chat_flows(request: Request) -> dict[str, object]:
        program = request.app.state.get_agent_state().program
        return {
            "default": None,
            "items": [{"name": flow.name} for flow in program.flows],
        }

    @router.post("/chat/stream", summary="Submit Chat Stream")
    async def submit_chat_stream(
        request: Request,
        payload: ChatRequest,
    ) -> ShutdownAwareStreamingResponse:
        context = request.app.state
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
    context: ComponentState,
    payload: ChatRequest,
    *,
    thread_id: str | None,
) -> tuple[RunRecord, BufferedReplySink]:
    _require_chat_runner(context)
    reply = BufferedReplySink()
    run_id = allocate_run_id(context.root, context.name)

    record = await context.executor.run(
        _chat_run_request(payload, thread_id=thread_id, run_id=run_id),
        context.get_agent_state(),
        reply=reply,
    )
    return record, reply


async def _stream_chat_run(
    context: ComponentState,
    payload: ChatRequest,
    *,
    thread_id: str | None,
):
    _require_chat_runner(context)
    reply = TraceReplySink() if payload.client == "tui" else SseReplySink(thread_id=thread_id)
    run_id = allocate_run_id(context.root, context.name)
    context.executor.start(
        _chat_run_request(payload, thread_id=thread_id, run_id=run_id),
        context.get_agent_state(),
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


def _require_chat_runner(context: ComponentState) -> None:
    enabled_components = context.config.require("components.enabled")
    if not isinstance(enabled_components, tuple) or "runner.chat" not in enabled_components:
        raise HTTPException(status_code=403, detail="component is not enabled: runner.chat")


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


def _chat_thread_id_or_404(context: ComponentState, payload: ChatRequest) -> str | None:
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
    return _peer_payload(payload.peer)


def _peer_payload(payload: ThreadPeerPayload | None) -> ThreadPeer | None:
    if payload is None:
        return None
    try:
        return ThreadPeer.from_data(payload.model_dump())
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


def _executable_kind(payload: ChatRequest) -> Literal["agic", "flow"]:
    if _text_or_none(payload.agic) is not None and _text_or_none(payload.flow) is not None:
        raise HTTPException(status_code=422, detail="chat request cannot specify both agic and flow")
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


def _thread_info(context: ComponentState, thread_id: str):
    runs = context.store.list_thread_runs_chronological(thread_id=thread_id)
    thread = context.store.get_thread(thread_id=thread_id)
    if not runs:
        if thread is None:
            raise HTTPException(status_code=500, detail=f"thread not found after completion: {thread_id}")
        return thread_info_from_record(thread)
    steps_by_run = context.store.list_steps_for_runs(run_ids=tuple(run.run_id for run in runs))
    commands_by_run = {run.run_id: context.store.list_commands(run_id=run.run_id) for run in runs}
    return thread_info_from_runs(
        thread_id,
        runs,
        commands_by_run=commands_by_run,
        steps_by_run=steps_by_run,
        thread=thread,
    )


def _new_thread_id(context: ComponentState, client: str) -> str:
    return allocate_thread_id(context.root, context.name, client)


def _chat_model_item(*, selector: str, target: Any) -> dict[str, object]:
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


def _default_agic_name(program: Any, *, origin: str) -> str | None:
    try:
        return select_origin_agic(program, origin=origin).name
    except ToolangError:
        return None
