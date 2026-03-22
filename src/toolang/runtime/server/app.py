from __future__ import annotations

import asyncio
import platform
from contextlib import asynccontextmanager
from datetime import timezone
from pathlib import Path
from typing import AsyncIterator, Awaitable, Callable, Iterator

import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse

from toolang.agent.prepared import PreparedAgent, prepare_agent
from toolang.agent.registry import get_running_agent
from toolang.bus.db import BusStore
from toolang.bus.events import utc_now
from toolang.caps import load_prepared_caps
from toolang.concepts.layout import AgentHome
from toolang.concepts.sandbox import SandboxSpec
from toolang.errors import ToolangError
from toolang.agent.api import add_cors

from ..api_models import (
    AgentCapsResponse,
    AgentProfile,
    AgentRuntimeResponse,
    ChatRequest,
    ChatResponse,
    ChatThreadListResponse,
    ChatThreadResponse,
    EventListResponse,
    RunDetailResponse,
    RunListResponse,
    RunRequest,
    RunResponse,
)
from ..build import infer_model
from ..chats import ChatMessage, ChatStore
from ..invoke import chat_prepared_agent, invoke_prepared_agent
from ..messages import chat_message
from .presenters import (
    SHORT_AGENT_ID_LENGTH,
    caps_response,
    data_sse,
    event_item,
    fallback_agent_snapshot,
    message_item,
    run_item,
    sse,
    thread_item,
    turn_item,
)
from .state import (
    activate_running_agent,
    deactivate_running_agent,
    has_running_state,
    touch_running_agent,
)

SSE_POLL_INTERVAL_SEC = 0.5
SSE_PING_INTERVAL_SEC = 20.0


def serve_agent(
    prepared: PreparedAgent,
    *,
    agents_db_path: Path,
    bus_db_path: Path,
    host: str,
    port: int,
    sandbox: str = "host",
    public_host: str | None = None,
    cors_allow_origins: list[str] | None = None,
) -> None:
    sandbox_spec = SandboxSpec.parse(sandbox).spec
    endpoint_host = public_host or host
    endpoint = f"http://{endpoint_host}:{port}"
    app = create_agent_app(
        prepared,
        agents_db_path=agents_db_path,
        bus_db_path=bus_db_path,
        host=host,
        port=port,
        sandbox=sandbox_spec,
        public_host=endpoint_host,
        cors_allow_origins=cors_allow_origins,
    )
    try:
        uvicorn.run(app, host=host, port=port, log_level="info")
    finally:
        if has_running_state(prepared, agents_db_path=agents_db_path):
            bus = BusStore(bus_db_path)
            try:
                deactivate_running_agent(
                    prepared,
                    agents_db_path=agents_db_path,
                    bus=bus,
                    endpoint=endpoint,
                    sandbox=sandbox_spec,
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
    sandbox: str = "host",
    public_host: str | None = None,
    cors_allow_origins: list[str] | None = None,
) -> FastAPI:
    parsed_sandbox = SandboxSpec.parse(sandbox)
    sandbox_spec = parsed_sandbox.spec
    endpoint_host = public_host or host
    endpoint = f"http://{endpoint_host}:{port}"
    bus = BusStore(bus_db_path)
    chats = ChatStore(AgentHome.resolve(prepared.ref.home).room(prepared.ref.name).chats_db_path)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        activate_running_agent(
            prepared,
            agents_db_path=agents_db_path,
            bus=bus,
            endpoint=endpoint,
            sandbox=sandbox_spec,
        )
        try:
            yield
        finally:
            try:
                deactivate_running_agent(
                    prepared,
                    agents_db_path=agents_db_path,
                    bus=bus,
                    endpoint=endpoint,
                    sandbox=sandbox_spec,
                )
            finally:
                chats.close()
                bus.close()

    app = FastAPI(
        title=f"Toolang Agent Server: {prepared.ref.name}",
        lifespan=lifespan,
    )
    add_cors(app, allow_origins=cors_allow_origins)

    @app.middleware("http")
    async def update_heartbeat(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        response = await call_next(request)
        touch_running_agent(prepared, agents_db_path=agents_db_path, endpoint=endpoint)
        return response

    @app.get("/healthz")
    def healthz() -> dict[str, object]:
        return {"ok": True, "agent": prepared.ref.name}

    @app.get("/api/v1/health")
    @app.get("/health")
    def health() -> dict[str, str]:
        return {
            "status": "ok",
            "agent_uri": prepared.ref.uri,
            "agent_id": prepared.ref.id[:SHORT_AGENT_ID_LENGTH],
            "agent_name": prepared.ref.name,
            "endpoint": endpoint,
        }

    @app.get("/api/v1/agent")
    @app.get("/agent", response_model_exclude_none=True)
    def agent_info():
        snapshot = bus.get_agent(prepared.ref.uri)
        if snapshot is not None:
            return snapshot
        return fallback_agent_snapshot(
            prepared,
            endpoint=endpoint,
            sandbox=sandbox_spec,
            now=utc_now(),
        )

    @app.get("/api/v1/profile", response_model=AgentProfile)
    def profile() -> AgentProfile:
        return AgentProfile(agent=prepared.ref.name)

    @app.get("/api/v1/runtime", response_model=AgentRuntimeResponse)
    def runtime_info() -> AgentRuntimeResponse:
        current_run = get_running_agent(agents_db_path, prepared.ref.uri)
        current = prepare_agent(prepared.ref, cap_scopes=prepared.cap_scopes)
        return AgentRuntimeResponse(
            status="online",
            checked_at=utc_now(),
            endpoint=endpoint,
            execution_host=parsed_sandbox.execution_host,
            working_directory=str(prepared.ref.home),
            sandbox=sandbox_spec,
            network="enabled",
            approvals="n/a",
            filesystem_scope="agent-home",
            os=platform.system(),
            arch=platform.machine(),
            runtime="python",
            runtime_version=platform.python_version(),
            started_at=(
                current_run.started_at.astimezone(timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                )
                if current_run is not None
                else None
            ),
            model=_default_model(current),
        )

    @app.get("/api/v1/caps", response_model=AgentCapsResponse)
    def list_caps() -> AgentCapsResponse:
        current = prepare_agent(prepared.ref, cap_scopes=prepared.cap_scopes)
        caps = load_prepared_caps(current)
        return caps_response(prepared.ref.name, caps)

    @app.get("/api/v1/chats", response_model=ChatThreadListResponse)
    def list_chats(limit: int = Query(50, ge=1, le=500)) -> ChatThreadListResponse:
        return ChatThreadListResponse(
            items=[
                thread_item(item)
                for item in chats.list_threads(agent_uri=prepared.ref.uri, limit=limit)
            ]
        )

    @app.get("/api/v1/chats/{thread_id}", response_model=ChatThreadResponse)
    def get_chat(thread_id: str, limit: int = Query(50, ge=1, le=500)) -> ChatThreadResponse:
        thread = chats.get_thread(thread_id=thread_id)
        if thread is None or thread.agent_uri != prepared.ref.uri:
            raise HTTPException(status_code=404, detail="thread not found")
        turns = chats.recent_turns(thread_id=thread_id, limit=limit)
        return ChatThreadResponse(
            thread=thread_item(thread),
            turns=[turn_item(item) for item in turns],
        )

    @app.get("/api/v1/runs", response_model=RunListResponse)
    def list_runs(limit: int = Query(50, ge=1, le=500)) -> RunListResponse:
        runs = bus.list_runs(agent_uri=prepared.ref.uri, limit=limit)
        return RunListResponse(items=[run_item(item) for item in runs])

    @app.get("/api/v1/runs/{run_id}", response_model=RunDetailResponse)
    def get_run(run_id: str) -> RunDetailResponse:
        run = bus.get_run(run_id)
        if run is None or run.agent_uri != prepared.ref.uri:
            raise HTTPException(status_code=404, detail="run not found")
        children = [
            item
            for item in bus.list_runs(agent_uri=prepared.ref.uri, limit=500)
            if item.parent_run_id == run_id
        ]
        events = bus.list_events(agent_uri=prepared.ref.uri, run_id=run_id, limit=500)
        turn = None
        if run.thread_id:
            loaded = chats.get_turn(thread_id=run.thread_id, turn_id=run_id)
            if loaded is not None:
                turn = turn_item(loaded)
        return RunDetailResponse(
            run=run_item(run),
            children=[run_item(item) for item in children],
            events=[event_item(item) for item in events],
            turn=turn,
        )

    @app.get("/api/v1/events", response_model=EventListResponse)
    def list_events(
        from_event_id: int = Query(0, ge=0),
        limit: int = Query(100, ge=1, le=2000),
    ) -> EventListResponse:
        events = bus.list_events(
            agent_uri=prepared.ref.uri,
            from_event_id=from_event_id,
            limit=limit,
        )
        return EventListResponse(items=[event_item(item) for item in events])

    @app.get("/api/v1/events/stream")
    async def stream_events(request: Request) -> StreamingResponse:
        async def stream() -> AsyncIterator[str]:
            min_event_id = await asyncio.to_thread(
                bus.max_event_id,
                agent_uri=prepared.ref.uri,
            )
            last_emitted = min_event_id
            last_ping_at = asyncio.get_running_loop().time()
            yield sse(
                "sub_ready",
                {
                    "min_event_id": min_event_id,
                    "agent_id": prepared.ref.id[:SHORT_AGENT_ID_LENGTH],
                },
            )
            while True:
                if await request.is_disconnected():
                    break
                rows = await asyncio.to_thread(
                    bus.list_events,
                    agent_uri=prepared.ref.uri,
                    from_event_id=last_emitted,
                    limit=200,
                )
                emitted = False
                for row in rows:
                    last_emitted = row.event_id
                    yield sse("event", event_item(row).model_dump(mode="json"), event_id=row.event_id)
                    emitted = True
                now = asyncio.get_running_loop().time()
                if (not emitted) and (now - last_ping_at >= SSE_PING_INTERVAL_SEC):
                    yield sse("ping", {})
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
            sandbox=sandbox_spec,
        )
        item = message_item(assistant)
        return ChatResponse(
            thread_id=assistant.thread_id,
            turn_id=assistant.turn_id,
            message=item,
            assistant=item,
        )

    @app.post("/api/v1/chat/stream")
    def chat_stream(request: ChatRequest) -> StreamingResponse:
        def stream() -> Iterator[str]:
            yield data_sse({"type": "start"})
            try:
                assistant = _chat_once(
                    prepared=prepared,
                    chats=chats,
                    bus_db_path=bus_db_path,
                    request=request,
                    sandbox=sandbox_spec,
                )
            except Exception as exc:
                message = str(exc).strip() or type(exc).__name__
                yield data_sse({"type": "error", "errorText": message})
                yield data_sse({"type": "finish"})
                yield "data: [DONE]\n\n"
                return

            yield data_sse({"type": "text-start", "id": assistant.turn_id})
            if assistant.text:
                yield data_sse(
                    {
                        "type": "text-delta",
                        "id": assistant.turn_id,
                        "delta": assistant.text,
                    }
                )
            yield data_sse({"type": "text-end", "id": assistant.turn_id})
            yield data_sse({"type": "finish"})
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
        current = prepare_agent(prepared.ref, cap_scopes=prepared.cap_scopes)
        try:
            selected_thunk = current.program.get_thunk(request.thunk)
            result = invoke_prepared_agent(
                current,
                selected_thunk,
                bus_db_path=bus_db_path,
                user_input=request.input,
                model=request.model,
                sandbox=sandbox_spec,
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
    sandbox: str,
) -> ChatMessage:
    thread_id = request.thread.strip()
    if not thread_id:
        raise ToolangError("Chat thread may not be empty.")
    text = request.message.strip()
    if not text:
        raise ToolangError("Chat message may not be empty.")

    current = prepare_agent(prepared.ref, cap_scopes=prepared.cap_scopes)
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
        sandbox=sandbox,
    )
    return result.assistant


def _select_chat_thunk(prepared: PreparedAgent, thunk_name: str | None):
    if thunk_name is not None:
        return prepared.program.get_thunk(thunk_name)
    try:
        return prepared.program.get_thunk("chat")
    except ToolangError:
        return prepared.program.default_thunk()


def _default_model(prepared: PreparedAgent) -> str | None:
    try:
        return infer_model(prepared.program.default_thunk())
    except ToolangError:
        return None
