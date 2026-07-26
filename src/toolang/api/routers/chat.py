"""Chat submission routes."""

from typing import cast

from fastapi import APIRouter, HTTPException

from toolang.api.app import AgentCoreDep
from toolang.api.conversion import parse_user_message
from toolang.api.schemas import ChatRequest, ChatResult
from toolang.base.types.message import Message, Percept
from toolang.execution.executor import RunSpec
from toolang.execution.records import ThreadPeer
from toolang.execution.types import ThreadPrefix
from toolang.up import AgentCore

from .threads import parse_thread_peer, thread_info

router = APIRouter(prefix="/chat", tags=["chat"])


@router.post("", summary="Submit Chat", response_model=ChatResult)
async def submit_chat(core: AgentCoreDep, payload: ChatRequest) -> ChatResult:
    thread_id = _chat_thread_id_or_404(core, payload)
    user_message = parse_user_message(payload.message)
    handle = core.executor.start(
        RunSpec(
            setup=core.setup.current(),
            state=core.state.current(),
            thread=thread_id,
            runnable=_runnable(core, payload),
            input=cast(Percept, user_message.parts),
            model=payload.model,
        ),
        request_id=payload.request_id,
    )
    record = await handle
    detail = core.history.get_run(record.id)
    if detail is None:
        raise HTTPException(
            status_code=500,
            detail=f"run not found after completion: {record.id}",
        )
    output = core.store.run_output(run_id=record.id)
    if not output:
        detail_message = detail.error or f"incomplete chat transcript: {record.id}"
        raise HTTPException(status_code=500, detail=detail_message)
    try:
        assistant = Message(role="assistant", parts=output)
    except ValueError as exc:
        raise HTTPException(
            status_code=500,
            detail=f"chat output is not an assistant message: {record.id}",
        ) from exc
    return ChatResult(
        thread=thread_info(core, thread_id),
        run=detail,
        message=user_message,
        assistant=assistant,
    )


@router.post("/stream", summary="Submit Chat Stream")
async def submit_chat_stream(core: AgentCoreDep, payload: ChatRequest) -> None:
    del core, payload
    raise HTTPException(
        status_code=501,
        detail="chat streaming is being migrated to the canonical run event protocol",
    )


def _chat_thread_id_or_404(core: AgentCore, payload: ChatRequest) -> str:
    if payload.thread is None:
        return core.threads.create(
            prefix=_thread_prefix(payload.client),
            peer=_request_peer(payload),
        )
    thread = core.store.get_thread(thread_id=payload.thread)
    if thread is None or thread.origin != "chat":
        raise HTTPException(
            status_code=404,
            detail=f"chat thread not found: {payload.thread}",
        )
    peer = _request_peer(payload)
    if peer is not None and thread.peer != peer:
        raise HTTPException(
            status_code=409,
            detail=f"chat thread peer mismatch: {payload.thread}",
        )
    return payload.thread


def _request_peer(payload: ChatRequest) -> ThreadPeer | None:
    return parse_thread_peer(payload.peer)


def _runnable(core: AgentCore, payload: ChatRequest) -> str:
    agic = _text_or_none(payload.agic)
    flow = _text_or_none(payload.flow)
    if agic is not None and flow is not None:
        raise HTTPException(
            status_code=422,
            detail="chat request cannot specify both agic and flow",
        )
    if flow is not None:
        return flow
    if agic is not None:
        return agic
    program = core.state.current().program
    return "chat" if program.find_agic("chat") is not None else "default"


def _thread_prefix(client: str) -> ThreadPrefix:
    if client == "web":
        return ThreadPrefix.WEB
    return ThreadPrefix.TERM


def _text_or_none(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None
