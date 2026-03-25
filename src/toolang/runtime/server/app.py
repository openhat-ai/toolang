from __future__ import annotations

import asyncio
import platform
from contextlib import asynccontextmanager
from datetime import timezone
from pathlib import Path
from queue import Empty, Queue
from typing import AsyncIterator, Awaitable, Callable, Iterator

import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from toolang.agent.prepared import PreparedAgent, prepare_agent
from toolang.agent.registry import get_running_agent
from toolang.bus.events import utc_now
from toolang.caps import load_prepared_caps
from toolang.concepts.execution import RuntimeLoop
from toolang.concepts.layout import AgentHome
from toolang.concepts.persisted import ChannelsConfig, PromptTrace
from toolang.concepts.sandbox import SandboxSpec
from toolang.errors import ToolangError
from toolang.web import add_cors

from ..api_models import (
    AgentCapsResponse,
    AgentProfile,
    AgentRuntimeResponse,
    ChatRequest,
    ChoreItem,
    ChorePatchRequest,
    ChorePutRequest,
    ChoreListResponse,
    ChatResponse,
    ChatThreadListResponse,
    ChatThreadResponse,
    EventListResponse,
    PromptTraceItem,
    RuntimeDiagnosticsResponse,
    RuntimeSecurityResponse,
    RunDetailResponse,
    RunListResponse,
    RunRequest,
    RunResponse,
    TaskListResponse,
    TaskPatchRequest,
    TaskPutRequest,
    TaskItem,
    WillPatchRequest,
    WillPutRequest,
    WillResponse,
)
from ..build import infer_model
from ..host import RuntimeHost
from ..model_exec import TextDeltaEvent, ToolCallFinishEvent, ToolCallStartEvent
from ..work import (
    list_chore_items,
    list_task_items,
    load_will_item,
    patch_chore_item,
    patch_task_item,
    patch_will_item,
    put_chore_item,
    put_task_item,
    put_will_item,
)
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

SSE_POLL_INTERVAL_SEC = 0.5
SSE_PING_INTERVAL_SEC = 20.0


def run_agent(
    prepared: PreparedAgent,
    *,
    agents_db_path: Path,
    bus_db_path: Path,
    host: str,
    port: int,
    sandbox: str = "host",
    public_host: str | None = None,
    cors_allow_origins: list[str] | None = None,
    runtime_loops: tuple[RuntimeLoop, ...] = ("server",),
    channels_config: ChannelsConfig | None = None,
) -> None:
    app = create_agent_app(
        prepared,
        agents_db_path=agents_db_path,
        bus_db_path=bus_db_path,
        host=host,
        port=port,
        sandbox=sandbox,
        public_host=public_host,
        cors_allow_origins=cors_allow_origins,
        runtime_loops=runtime_loops,
        channels_config=channels_config,
    )
    uvicorn.run(app, host=host, port=port, log_level="info")


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
    runtime_loops: tuple[RuntimeLoop, ...] = ("server",),
    channels_config: ChannelsConfig | None = None,
) -> FastAPI:
    runtime_host = RuntimeHost(
        prepared,
        agents_db_path=agents_db_path,
        bus_db_path=bus_db_path,
        host=host,
        port=port,
        sandbox=sandbox,
        public_host=public_host,
        runtime_loops=runtime_loops,
        channels_config=channels_config,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        runtime_host.start()
        try:
            yield
        finally:
            runtime_host.stop()

    app = FastAPI(
        title=f"Toolang Agent API: {prepared.ref.name}",
        lifespan=lifespan,
    )
    add_cors(app, allow_origins=cors_allow_origins)

    @app.middleware("http")
    async def update_heartbeat(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        response = await call_next(request)
        runtime_host.touch()
        return response

    @app.exception_handler(ToolangError)
    async def handle_toolang_error(_: Request, exc: ToolangError) -> JSONResponse:
        return JSONResponse(status_code=400, content={"detail": str(exc)})

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
            "endpoint": runtime_host.endpoint,
        }

    @app.get("/api/v1/agent")
    @app.get("/agent", response_model_exclude_none=True)
    def agent_info():
        snapshot = runtime_host.bus.get_agent(prepared.ref.uri)
        if snapshot is not None:
            return snapshot
        return fallback_agent_snapshot(
            prepared,
            endpoint=runtime_host.endpoint,
            sandbox=runtime_host.sandbox,
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
            endpoint=runtime_host.endpoint,
            execution_host=SandboxSpec.parse(runtime_host.sandbox).execution_host,
            working_directory=str(prepared.ref.home),
            sandbox=runtime_host.sandbox,
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
            security=RuntimeSecurityResponse.model_validate(
                runtime_host.security_snapshot(prepared=current)
            ),
        )

    @app.get("/api/v1/caps", response_model=AgentCapsResponse)
    def list_caps() -> AgentCapsResponse:
        current = prepare_agent(prepared.ref, cap_scopes=prepared.cap_scopes)
        caps = load_prepared_caps(current)
        return caps_response(prepared.ref.name, caps)

    @app.get("/api/v1/tasks", response_model=TaskListResponse)
    def list_tasks() -> TaskListResponse:
        room = AgentHome.resolve(prepared.ref.home).room(prepared.ref.name)
        return TaskListResponse(items=list_task_items(room))

    @app.put("/api/v1/tasks/{task_name:path}", response_model=TaskItem)
    def put_task(task_name: str, request: TaskPutRequest) -> TaskItem:
        room = AgentHome.resolve(prepared.ref.home).room(prepared.ref.name)
        return put_task_item(room, task_name, request)

    @app.patch("/api/v1/tasks/{task_name:path}", response_model=TaskItem)
    def patch_task(task_name: str, request: TaskPatchRequest) -> TaskItem:
        room = AgentHome.resolve(prepared.ref.home).room(prepared.ref.name)
        return patch_task_item(room, task_name, request)

    @app.get("/api/v1/chores", response_model=ChoreListResponse)
    def list_chores() -> ChoreListResponse:
        room = AgentHome.resolve(prepared.ref.home).room(prepared.ref.name)
        return ChoreListResponse(items=list_chore_items(room))

    @app.put("/api/v1/chores/{chore_id:path}", response_model=ChoreItem)
    def put_chore(chore_id: str, request: ChorePutRequest) -> ChoreItem:
        room = AgentHome.resolve(prepared.ref.home).room(prepared.ref.name)
        return put_chore_item(room, chore_id, request)

    @app.patch("/api/v1/chores/{chore_id:path}", response_model=ChoreItem)
    def patch_chore(chore_id: str, request: ChorePatchRequest) -> ChoreItem:
        room = AgentHome.resolve(prepared.ref.home).room(prepared.ref.name)
        return patch_chore_item(room, chore_id, request)

    @app.get("/api/v1/will", response_model=WillResponse)
    def get_will() -> WillResponse:
        room = AgentHome.resolve(prepared.ref.home).room(prepared.ref.name)
        return WillResponse(item=load_will_item(room, agent=prepared.ref))

    @app.put("/api/v1/will", response_model=WillResponse)
    def put_will(request: WillPutRequest) -> WillResponse:
        room = AgentHome.resolve(prepared.ref.home).room(prepared.ref.name)
        return WillResponse(item=put_will_item(room, request, agent=prepared.ref))

    @app.patch("/api/v1/will", response_model=WillResponse)
    def patch_will(request: WillPatchRequest) -> WillResponse:
        room = AgentHome.resolve(prepared.ref.home).room(prepared.ref.name)
        return WillResponse(item=patch_will_item(room, request, agent=prepared.ref))

    @app.get("/api/v1/chats", response_model=ChatThreadListResponse)
    def list_chats(limit: int = Query(50, ge=1, le=500)) -> ChatThreadListResponse:
        return ChatThreadListResponse(
            items=[
                thread_item(item)
                for item in runtime_host.chats.list_threads(
                    agent_uri=prepared.ref.uri, limit=limit
                )
            ]
        )

    @app.get("/api/v1/chats/{thread_id}", response_model=ChatThreadResponse)
    def get_chat(
        thread_id: str, limit: int = Query(50, ge=1, le=500)
    ) -> ChatThreadResponse:
        thread = runtime_host.chats.get_thread(thread_id=thread_id)
        if thread is None or thread.agent_uri != prepared.ref.uri:
            raise HTTPException(status_code=404, detail="thread not found")
        room = AgentHome.resolve(prepared.ref.home).room(prepared.ref.name)
        turns = runtime_host.chats.recent_turns(thread_id=thread_id, limit=limit)
        return ChatThreadResponse(
            thread=thread_item(thread),
            turns=[
                turn_item(item, tool_calls=_turn_tool_calls(room, item.turn_id))
                for item in turns
            ],
        )

    @app.get("/api/v1/runs", response_model=RunListResponse)
    def list_runs(limit: int = Query(50, ge=1, le=500)) -> RunListResponse:
        runs = runtime_host.bus.list_runs(agent_uri=prepared.ref.uri, limit=limit)
        return RunListResponse(items=[run_item(item) for item in runs])

    @app.get("/api/v1/runs/{run_id}", response_model=RunDetailResponse)
    def get_run(run_id: str) -> RunDetailResponse:
        run = runtime_host.bus.get_run(run_id)
        if run is None or run.agent_uri != prepared.ref.uri:
            raise HTTPException(status_code=404, detail="run not found")
        children = [
            item
            for item in runtime_host.bus.list_runs(
                agent_uri=prepared.ref.uri, limit=500
            )
            if item.parent_run_id == run_id
        ]
        events = runtime_host.bus.list_events(
            agent_uri=prepared.ref.uri, run_id=run_id, limit=500
        )
        turn = None
        if run.thread_id:
            room = AgentHome.resolve(prepared.ref.home).room(prepared.ref.name)
            loaded = runtime_host.chats.get_turn(
                thread_id=run.thread_id, turn_id=run_id
            )
            if loaded is not None:
                turn = turn_item(
                    loaded,
                    tool_calls=_turn_tool_calls(room, loaded.turn_id),
                )
        return RunDetailResponse(
            run=run_item(run),
            children=[run_item(item) for item in children],
            events=[event_item(item) for item in events],
            turn=turn,
        )

    @app.get("/api/v1/runs/{run_id}/prompt", response_model=PromptTraceItem)
    def get_run_prompt(run_id: str) -> PromptTraceItem:
        room = AgentHome.resolve(prepared.ref.home).room(prepared.ref.name)
        trace_path = room.prompt_trace_path(run_id)
        if not trace_path.exists():
            raise HTTPException(status_code=404, detail="prompt trace not found")
        trace = PromptTrace.load(trace_path)
        return PromptTraceItem.model_validate(trace.model_dump(mode="python"))

    @app.get("/api/v1/events", response_model=EventListResponse)
    def list_events(
        from_event_id: int = Query(0, ge=0),
        limit: int = Query(100, ge=1, le=2000),
    ) -> EventListResponse:
        events = runtime_host.bus.list_events(
            agent_uri=prepared.ref.uri,
            from_event_id=from_event_id,
            limit=limit,
        )
        return EventListResponse(items=[event_item(item) for item in events])

    @app.get("/api/v1/events/stream")
    async def stream_events(request: Request) -> StreamingResponse:
        async def stream() -> AsyncIterator[str]:
            min_event_id = await asyncio.to_thread(
                runtime_host.bus.max_event_id,
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
                    runtime_host.bus.list_events,
                    agent_uri=prepared.ref.uri,
                    from_event_id=last_emitted,
                    limit=200,
                )
                emitted = False
                for row in rows:
                    last_emitted = row.event_id
                    yield sse(
                        "event",
                        event_item(row).model_dump(mode="json"),
                        event_id=row.event_id,
                    )
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

    @app.get("/api/v1/diagnostics", response_model=RuntimeDiagnosticsResponse)
    @app.get("/api/v1/runtime/diagnostics", response_model=RuntimeDiagnosticsResponse)
    def runtime_diagnostics() -> RuntimeDiagnosticsResponse:
        return RuntimeDiagnosticsResponse.model_validate(
            runtime_host.diagnostics_snapshot()
        )

    @app.post("/api/v1/chat", response_model=ChatResponse)
    def chat(request: ChatRequest) -> ChatResponse:
        assistant = runtime_host.submit_chat(request).assistant
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
            event_queue: Queue[object] = Queue()
            text_started = False
            try:
                turn_id, future = runtime_host.submit_chat_stream(
                    request,
                    event_queue.put,
                )
                while True:
                    try:
                        event = event_queue.get(timeout=0.05)
                    except Empty:
                        if future.done():
                            break
                        continue
                    if isinstance(event, TextDeltaEvent):
                        if not text_started:
                            text_started = True
                            yield data_sse({"type": "text-start", "id": turn_id})
                        yield data_sse(
                            {
                                "type": "text-delta",
                                "id": turn_id,
                                "delta": event.delta,
                            }
                        )
                        continue
                    if isinstance(event, ToolCallStartEvent):
                        yield data_sse(
                            {
                                "type": "tool-call-start",
                                "id": turn_id,
                                "tool_call_id": event.call_id,
                                "family": event.family,
                                "name": event.name,
                                "arguments": event.arguments,
                            }
                        )
                        continue
                    if isinstance(event, ToolCallFinishEvent):
                        yield data_sse(
                            {
                                "type": "tool-call-finish",
                                "id": turn_id,
                                "tool_call_id": event.call_id,
                                "family": event.result.family,
                                "name": event.result.name,
                                "output": event.result.output,
                                "error": event.result.error,
                            }
                        )
                assistant = future.result().assistant
            except Exception as exc:
                message = str(exc).strip() or type(exc).__name__
                yield data_sse({"type": "error", "errorText": message})
                yield data_sse({"type": "finish"})
                yield "data: [DONE]\n\n"
                return

            if assistant.text and not text_started:
                text_started = True
                yield data_sse({"type": "text-start", "id": assistant.turn_id})
                yield data_sse(
                    {
                        "type": "text-delta",
                        "id": assistant.turn_id,
                        "delta": assistant.text,
                    }
                )
            if text_started:
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
        try:
            result = runtime_host.submit_run(request)
        except ToolangError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return RunResponse(run_id=result.run_id, output=result.output)

    return app


def _default_model(prepared: PreparedAgent) -> str | None:
    try:
        return infer_model(prepared.program.default_thunk())
    except ToolangError:
        return None


def _turn_tool_calls(room, turn_id: str) -> list[dict[str, object]]:
    trace_path = room.prompt_trace_path(turn_id)
    if not trace_path.exists():
        return []
    try:
        trace = PromptTrace.load(trace_path)
    except Exception:
        return []
    return [dict(item) for item in trace.tool_calls]
