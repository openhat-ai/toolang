from __future__ import annotations

import asyncio
import os
import platform
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator, Awaitable, Callable, Iterator

import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse

from toolang.api_models import (
    AgentCapsResponse,
    AgentChatMessage,
    AgentProfile,
    AgentRuntimeResponse,
    CapItem,
    ChatRequest,
    ChatResponse,
    ChatThreadItem,
    ChatThreadListResponse,
    ChatThreadResponse,
    ChatTurnItem,
    EventItem,
    EventListResponse,
    RunDetailResponse,
    RunItem,
    RunListResponse,
    RunRequest,
    RunResponse,
)
from toolang.agent_registry import (
    KnownAgentRecord,
    RunningAgentRecord,
    delete_running_agent,
    get_running_agent,
    upsert_known_agent,
    upsert_running_agent,
)
from toolang.bus.db import AgentSnapshot, BusStore, RunSnapshot, StoredEvent
from toolang.bus.events import AgentStarted, AgentStopped, utc_now
from toolang.caps_view import InlineCapView, SkillCapView, load_prepared_caps
from toolang.chats import ChatMessage, ChatStore, ChatThread, ChatTurn
from toolang.errors import ToolangError
from toolang.files.agent_run import AgentRunState
from toolang.http import add_cors
from toolang.invoke import chat_prepared_agent, invoke_prepared_agent
from toolang.layout import agent_chats_db_path, agent_run_path
from toolang.messages import chat_message
from toolang.prepared import PreparedAgent, prepare_agent
from toolang.runtime import infer_model

SHORT_AGENT_ID_LENGTH = 12
SSE_POLL_INTERVAL_SEC = 0.5
SSE_PING_INTERVAL_SEC = 20.0


def serve_agent(
    prepared: PreparedAgent,
    *,
    agents_db_path: Path,
    bus_db_path: Path,
    host: str,
    port: int,
    cors_allow_origins: list[str] | None = None,
) -> None:
    endpoint = f"http://{host}:{port}"
    app = create_agent_app(
        prepared,
        agents_db_path=agents_db_path,
        bus_db_path=bus_db_path,
        host=host,
        port=port,
        cors_allow_origins=cors_allow_origins,
    )
    try:
        uvicorn.run(app, host=host, port=port, log_level="info")
    finally:
        if _has_running_state(prepared, agents_db_path=agents_db_path):
            bus = BusStore(bus_db_path)
            try:
                _deactivate_running_agent(
                    prepared,
                    agents_db_path=agents_db_path,
                    bus=bus,
                    endpoint=endpoint,
                )
            finally:
                bus.close()


def create_agent_app(
    prepared: PreparedAgent,
    *,
    agents_db_path: Path,
    bus_db_path: Path,
    host: str,
    port: int,
    cors_allow_origins: list[str] | None = None,
) -> FastAPI:
    endpoint = f"http://{host}:{port}"
    bus = BusStore(bus_db_path)
    chats = ChatStore(agent_chats_db_path(prepared.ref.agent_home, prepared.ref.agent_name))

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        _activate_running_agent(
            prepared,
            agents_db_path=agents_db_path,
            bus=bus,
            endpoint=endpoint,
        )
        try:
            yield
        finally:
            try:
                _deactivate_running_agent(
                    prepared,
                    agents_db_path=agents_db_path,
                    bus=bus,
                    endpoint=endpoint,
                )
            finally:
                chats.close()
                bus.close()

    app = FastAPI(
        title=f"Toolang Agent Server: {prepared.ref.agent_name}",
        lifespan=lifespan,
    )
    add_cors(app, allow_origins=cors_allow_origins)

    @app.middleware("http")
    async def update_heartbeat(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        response = await call_next(request)
        _touch_running_agent(prepared, agents_db_path=agents_db_path, endpoint=endpoint)
        return response

    @app.get("/healthz")
    def healthz() -> dict[str, object]:
        return {"ok": True, "agent": prepared.ref.agent_name}

    @app.get("/api/v1/health")
    @app.get("/health")
    def health() -> dict[str, str]:
        return {
            "status": "ok",
            "agent_uri": prepared.ref.agent_uri,
            "agent_id": prepared.ref.agent_id[:SHORT_AGENT_ID_LENGTH],
            "agent_name": prepared.ref.agent_name,
            "endpoint": endpoint,
        }

    @app.get("/api/v1/agent")
    @app.get("/agent", response_model_exclude_none=True)
    def agent_info() -> AgentSnapshot:
        snapshot = bus.get_agent(prepared.ref.agent_uri)
        if snapshot is not None:
            return snapshot
        now = utc_now()
        return AgentSnapshot(
            agent_uri=prepared.ref.agent_uri,
            agent_id=prepared.ref.agent_id[:SHORT_AGENT_ID_LENGTH],
            name=prepared.ref.agent_name,
            kind=prepared.ref.agent_kind,
            status="prepared",
            endpoint=endpoint,
            agent_home=str(prepared.ref.agent_home),
            source_file=prepared.source_path.name,
            detail=None,
            created_at=now,
            updated_at=now,
        )

    @app.get("/api/v1/profile", response_model=AgentProfile)
    def profile() -> AgentProfile:
        return AgentProfile(agent=prepared.ref.agent_name)

    @app.get("/api/v1/runtime", response_model=AgentRuntimeResponse)
    def runtime_info() -> AgentRuntimeResponse:
        current_run = get_running_agent(agents_db_path, prepared.ref.agent_uri)
        current = prepare_agent(prepared.ref)
        return AgentRuntimeResponse(
            status="online",
            checked_at=utc_now(),
            endpoint=endpoint,
            execution_host="local",
            working_directory=str(prepared.ref.agent_home),
            sandbox="none",
            network="enabled",
            approvals="n/a",
            filesystem_scope="agent-home",
            os=platform.system(),
            arch=platform.machine(),
            runtime="python",
            runtime_version=platform.python_version(),
            started_at=(
                current_run.started_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                if current_run is not None
                else None
            ),
            model=_default_model(current),
        )

    @app.get("/api/v1/caps", response_model=AgentCapsResponse)
    def list_caps() -> AgentCapsResponse:
        current = prepare_agent(prepared.ref)
        caps = load_prepared_caps(current)
        return _caps_response(prepared.ref.agent_name, caps)

    @app.get("/api/v1/chats", response_model=ChatThreadListResponse)
    def list_chats(limit: int = Query(50, ge=1, le=500)) -> ChatThreadListResponse:
        return ChatThreadListResponse(
            items=[_thread_item(item) for item in chats.list_threads(agent_uri=prepared.ref.agent_uri, limit=limit)]
        )

    @app.get("/api/v1/chats/{thread_id}", response_model=ChatThreadResponse)
    def get_chat(thread_id: str, limit: int = Query(50, ge=1, le=500)) -> ChatThreadResponse:
        thread = chats.get_thread(thread_id=thread_id)
        if thread is None or thread.agent_uri != prepared.ref.agent_uri:
            raise HTTPException(status_code=404, detail="thread not found")
        turns = chats.recent_turns(thread_id=thread_id, limit=limit)
        return ChatThreadResponse(
            thread=_thread_item(thread),
            turns=[_turn_item(item) for item in turns],
        )

    @app.get("/api/v1/runs", response_model=RunListResponse)
    def list_runs(limit: int = Query(50, ge=1, le=500)) -> RunListResponse:
        runs = bus.list_runs(agent_uri=prepared.ref.agent_uri, limit=limit)
        return RunListResponse(items=[_run_item(item) for item in runs])

    @app.get("/api/v1/runs/{run_id}", response_model=RunDetailResponse)
    def get_run(run_id: str) -> RunDetailResponse:
        run = bus.get_run(run_id)
        if run is None or run.agent_uri != prepared.ref.agent_uri:
            raise HTTPException(status_code=404, detail="run not found")
        children = [
            item
            for item in bus.list_runs(agent_uri=prepared.ref.agent_uri, limit=500)
            if item.parent_run_id == run_id
        ]
        events = bus.list_events(agent_uri=prepared.ref.agent_uri, run_id=run_id, limit=500)
        turn = None
        if run.thread_id:
            loaded = chats.get_turn(thread_id=run.thread_id, turn_id=run_id)
            if loaded is not None:
                turn = _turn_item(loaded)
        return RunDetailResponse(
            run=_run_item(run),
            children=[_run_item(item) for item in children],
            events=[_event_item(item) for item in events],
            turn=turn,
        )

    @app.get("/api/v1/events", response_model=EventListResponse)
    def list_events(
        from_event_id: int = Query(0, ge=0),
        limit: int = Query(100, ge=1, le=2000),
    ) -> EventListResponse:
        events = bus.list_events(
            agent_uri=prepared.ref.agent_uri,
            from_event_id=from_event_id,
            limit=limit,
        )
        return EventListResponse(items=[_event_item(item) for item in events])

    @app.get("/api/v1/events/stream")
    async def stream_events(request: Request) -> StreamingResponse:
        async def stream() -> AsyncIterator[str]:
            min_event_id = await asyncio.to_thread(bus.max_event_id, agent_uri=prepared.ref.agent_uri)
            last_emitted = min_event_id
            last_ping_at = asyncio.get_running_loop().time()
            yield _sse(
                "sub_ready",
                {"min_event_id": min_event_id, "agent_id": prepared.ref.agent_id[:SHORT_AGENT_ID_LENGTH]},
            )
            while True:
                if await request.is_disconnected():
                    break
                rows = await asyncio.to_thread(
                    bus.list_events,
                    agent_uri=prepared.ref.agent_uri,
                    from_event_id=last_emitted,
                    limit=200,
                )
                emitted = False
                for row in rows:
                    last_emitted = row.event_id
                    yield _sse("event", _event_item(row).model_dump(mode="json"), event_id=row.event_id)
                    emitted = True
                now = asyncio.get_running_loop().time()
                if (not emitted) and (now - last_ping_at >= SSE_PING_INTERVAL_SEC):
                    yield _sse("ping", {})
                    last_ping_at = now
                await asyncio.sleep(SSE_POLL_INTERVAL_SEC)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @app.post("/api/v1/chat", response_model=ChatResponse)
    def chat(request: ChatRequest) -> ChatResponse:
        assistant = _chat_once(
            prepared=prepared,
            chats=chats,
            bus_db_path=bus_db_path,
            request=request,
        )
        item = _message_item(assistant)
        return ChatResponse(
            thread_id=assistant.thread_id,
            turn_id=assistant.turn_id,
            message=item,
            assistant=item,
        )

    @app.post("/api/v1/chat/stream")
    def chat_stream(request: ChatRequest) -> StreamingResponse:
        def stream() -> Iterator[str]:
            yield _data_sse({"type": "start"})
            try:
                assistant = _chat_once(
                    prepared=prepared,
                    chats=chats,
                    bus_db_path=bus_db_path,
                    request=request,
                )
            except Exception as exc:
                message = str(exc).strip() or type(exc).__name__
                yield _data_sse({"type": "error", "errorText": message})
                yield _data_sse({"type": "finish"})
                yield "data: [DONE]\n\n"
                return

            yield _data_sse({"type": "text-start", "id": assistant.turn_id})
            if assistant.text:
                yield _data_sse(
                    {
                        "type": "text-delta",
                        "id": assistant.turn_id,
                        "delta": assistant.text,
                    }
                )
            yield _data_sse({"type": "text-end", "id": assistant.turn_id})
            yield _data_sse({"type": "finish"})
            yield "data: [DONE]\n\n"

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @app.post("/api/v1/runs", response_model=RunResponse)
    @app.post("/runs", response_model=RunResponse)
    def run_thunk(request: RunRequest) -> RunResponse:
        current = prepare_agent(prepared.ref)
        try:
            selected_thunk = current.program.get_thunk(request.thunk)
            result = invoke_prepared_agent(
                current,
                selected_thunk,
                bus_db_path=bus_db_path,
                user_input=request.input,
                model=request.model,
            )
        except ToolangError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return RunResponse(run_id=result.run_id, output=result.output)

    return app


def _chat_once(
    *,
    prepared: PreparedAgent,
    chats: ChatStore,
    bus_db_path: Path,
    request: ChatRequest,
) -> ChatMessage:
    thread_id = request.thread.strip()
    if not thread_id:
        raise ToolangError("Chat thread may not be empty.")
    text = request.message.strip()
    if not text:
        raise ToolangError("Chat message may not be empty.")

    current = prepare_agent(prepared.ref)
    selected_thunk = _select_chat_thunk(current, request.thunk)
    incoming = chat_message(
        channel="api",
        sender="owner",
        thread_id=thread_id,
        text=text,
    )
    result = chat_prepared_agent(
        current,
        selected_thunk,
        bus_db_path=bus_db_path,
        chat_store=chats,
        message=incoming,
        model=request.model,
    )
    return result.assistant


def _select_chat_thunk(prepared: PreparedAgent, thunk_name: str | None):
    if thunk_name is not None:
        return prepared.program.get_thunk(thunk_name)
    try:
        return prepared.program.get_thunk("chat")
    except ToolangError:
        return prepared.program.default_thunk()


def _activate_running_agent(
    prepared: PreparedAgent,
    *,
    agents_db_path: Path,
    bus: BusStore,
    endpoint: str,
) -> None:
    current_pid = os.getpid()
    existing = get_running_agent(agents_db_path, prepared.ref.agent_uri)
    if existing is not None and existing.pid != current_pid and _pid_exists(existing.pid):
        raise ToolangError(f"Agent is already being served: {prepared.ref.agent_uri}")

    now = datetime.now(timezone.utc)
    upsert_known_agent(
        agents_db_path,
        KnownAgentRecord.from_resolved_agent(prepared.ref, updated_at=now),
    )
    upsert_running_agent(
        agents_db_path,
        RunningAgentRecord(
            agent_uri=prepared.ref.agent_uri,
            pid=current_pid,
            status="running",
            endpoint=endpoint,
            started_at=now,
            heartbeat_at=now,
        ),
    )
    bus.append(
        AgentStarted(
            at=utc_now(),
            agent_uri=prepared.ref.agent_uri,
            agent_id=prepared.ref.agent_id[:SHORT_AGENT_ID_LENGTH],
            name=prepared.ref.agent_name,
            kind=prepared.ref.agent_kind,
            endpoint=endpoint,
            agent_home=str(prepared.ref.agent_home),
            source_file=prepared.source_path.name,
        )
    )
    _write_agent_run_state(
        prepared,
        endpoint=endpoint,
        status="running",
        started_at=now,
        heartbeat_at=now,
    )


def _touch_running_agent(
    prepared: PreparedAgent,
    *,
    agents_db_path: Path,
    endpoint: str,
) -> None:
    current = get_running_agent(agents_db_path, prepared.ref.agent_uri)
    if current is None:
        return
    now = datetime.now(timezone.utc)
    updated = current.model_copy(update={"heartbeat_at": now})
    upsert_running_agent(agents_db_path, updated)
    _write_agent_run_state(
        prepared,
        endpoint=endpoint,
        status=updated.status,
        started_at=updated.started_at,
        heartbeat_at=now,
    )


def _deactivate_running_agent(
    prepared: PreparedAgent,
    *,
    agents_db_path: Path,
    bus: BusStore,
    endpoint: str,
) -> None:
    current = get_running_agent(agents_db_path, prepared.ref.agent_uri)
    now = datetime.now(timezone.utc)
    started_at = current.started_at if current is not None else now
    delete_running_agent(agents_db_path, prepared.ref.agent_uri)
    bus.append(
        AgentStopped(
            at=utc_now(),
            agent_uri=prepared.ref.agent_uri,
            agent_id=prepared.ref.agent_id[:SHORT_AGENT_ID_LENGTH],
            name=prepared.ref.agent_name,
            detail="server stopped",
            endpoint=endpoint,
            agent_home=str(prepared.ref.agent_home),
            source_file=prepared.source_path.name,
        )
    )
    _write_agent_run_state(
        prepared,
        endpoint=endpoint,
        status="stopped",
        started_at=started_at,
        heartbeat_at=now,
    )


def _write_agent_run_state(
    prepared: PreparedAgent,
    *,
    endpoint: str,
    status: str,
    started_at: datetime,
    heartbeat_at: datetime,
) -> None:
    run_path = agent_run_path(prepared.ref.agent_home, prepared.ref.agent_name)
    run_path.parent.mkdir(parents=True, exist_ok=True)
    AgentRunState(
        agent_uri=prepared.ref.agent_uri,
        agent_id=prepared.ref.agent_id[:SHORT_AGENT_ID_LENGTH],
        agent_name=prepared.ref.agent_name,
        agent_home=str(prepared.ref.agent_home),
        source_file=prepared.source_path.name,
        pid=os.getpid(),
        status=status,
        endpoint=endpoint,
        started_at=started_at,
        heartbeat_at=heartbeat_at,
    ).save(run_path)


def _has_running_state(
    prepared: PreparedAgent,
    *,
    agents_db_path: Path,
) -> bool:
    if get_running_agent(agents_db_path, prepared.ref.agent_uri) is not None:
        return True
    run_path = agent_run_path(prepared.ref.agent_home, prepared.ref.agent_name)
    if not run_path.exists():
        return False
    return AgentRunState.load(run_path).status == "running"


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _default_model(prepared: PreparedAgent) -> str | None:
    try:
        return infer_model(prepared.program.default_thunk())
    except ToolangError:
        return None


def _caps_response(agent_name: str, caps) -> AgentCapsResponse:
    return AgentCapsResponse(
        agent=agent_name,
        psyches=[_psyche_item(item) for item in caps.psyches],
        skills=[_skill_item(item) for item in caps.skills],
        servers=[_service_item(item) for item in caps.services],
        chores=[],
        counts={
            "psyches": len(caps.psyches),
            "skills": len(caps.skills),
            "servers": len(caps.services),
            "chores": 0,
        },
    )


def _skill_item(item: SkillCapView) -> CapItem:
    return CapItem(name=item.name, source=item.ref, effective=item.path)


def _service_item(item: InlineCapView) -> CapItem:
    source = item.front_matter.get("target") if isinstance(item.front_matter, dict) else None
    return CapItem(name=item.name, source=_string_or_none(source), effective=item.path)


def _psyche_item(item: InlineCapView) -> CapItem:
    return CapItem(name=item.name, source=item.path, effective=item.path)


def _event_item(item: StoredEvent) -> EventItem:
    return EventItem(
        event_id=item.event_id,
        event_type=item.event_type,
        at=item.at,
        agent_id=item.agent_id,
        run_id=item.run_id,
        payload=dict(item.payload),
    )


def _run_item(item: RunSnapshot) -> RunItem:
    return RunItem(
        id=item.run_id,
        summary=item.summary,
        status=item.status,
        type=item.run_type,
        agent_id=item.agent_id,
        parent_run_id=item.parent_run_id,
        error=item.error,
        thread_id=item.thread_id,
        origin_kind=_origin_kind(item.origin),
        origin_actor=_origin_actor(item.origin),
        origin_subject=item.thread_id,
        display_title=item.summary,
        display_subtitle=_run_display_subtitle(item),
        created_at=item.created_at,
        updated_at=item.updated_at,
    )


def _run_display_subtitle(item: RunSnapshot) -> str | None:
    if item.thread_id:
        return f"{item.origin} · {item.thread_id}"
    return item.origin


def _origin_kind(origin: str) -> str:
    if origin in {"invoke", "chat"}:
        return "direct"
    return origin


def _origin_actor(origin: str) -> str:
    if origin in {"invoke", "chat"}:
        return "owner"
    return "self"


def _thread_item(thread: ChatThread) -> ChatThreadItem:
    return ChatThreadItem(
        id=thread.id,
        agent=thread.agent_name,
        title=thread.title,
        created_at=thread.created_at,
        updated_at=thread.updated_at,
    )


def _turn_item(turn: ChatTurn) -> ChatTurnItem:
    return ChatTurnItem(
        thread_id=turn.thread_id,
        turn_id=turn.turn_id,
        messages=[_message_item(message) for message in turn.messages],
        tool_calls=[],
        started_at=turn.started_at,
        finished_at=turn.finished_at,
    )


def _message_item(message: ChatMessage) -> AgentChatMessage:
    return AgentChatMessage(
        id=message.id,
        thread_id=message.thread_id,
        turn_id=message.turn_id,
        seq=message.seq,
        role=message.role,
        parts=[{"type": "text", "text": message.text}],
        created_at=message.created_at,
        meta=dict(message.meta),
    )


def _string_or_none(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _sse(event: str, data: dict[str, object], event_id: int | None = None) -> str:
    lines: list[str] = []
    if event_id is not None:
        lines.append(f"id: {event_id}")
    lines.append(f"event: {event}")
    lines.append("data: " + __import__("json").dumps(data, ensure_ascii=False, separators=(",", ":")))
    return "\n".join(lines) + "\n\n"


def _data_sse(chunk: dict[str, object]) -> str:
    return "data: " + __import__("json").dumps(chunk, ensure_ascii=False, separators=(",", ":")) + "\n\n"
