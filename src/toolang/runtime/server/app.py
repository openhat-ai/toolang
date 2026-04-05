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

from toolang.agent.prepared import PreparedAgent
from toolang.agent.registry import get_running_agent
from toolang.bus.events import utc_now
from toolang.caps import load_prepared_caps
from toolang.concepts.caps import CapKind
from toolang.concepts.execution import MessageOrigin, RunStatus, RuntimeLoop
from toolang.concepts.layout import AgentHome
from toolang.concepts.persisted import ChannelsConfig, PromptTrace
from toolang.concepts.sandbox import SandboxSpec
from toolang.errors import ExternalDependencyUnavailableError, ToolangError
from toolang.web import add_cors

from ..api_models import (
    AgentCapsResponse,
    AgentChatMessage,
    CapDetailResponse,
    CapDeleteResponse,
    CapListResponse,
    CapMutationResponse,
    CapPutRequest,
    AgentProfile,
    AgentRuntimeResponse,
    ChatRequest,
    ThreadListResponse,
    ThreadResponse,
    ChoreItem,
    ChorePatchRequest,
    ChorePutRequest,
    ChoreListResponse,
    ChatResponse,
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
from ..assembly import infer_model
from ..chat_protocol import AIMessageChunkEncoder, chunk_to_dict
from ..control import (
    delete_cap as control_delete_cap,
    patch_chore_item,
    patch_task_item,
    patch_will_item,
    put_cap as control_put_cap,
    put_chore_item,
    put_task_item,
    put_will_item,
)
from ..inspect import runtime_diagnostics_snapshot, runtime_security_snapshot
from ..model_exec import TextDeltaEvent
from ..process import RuntimeProcess
from ..work import list_chore_items, list_task_items, load_will_item
from .presenters import (
    SHORT_AGENT_ID_LENGTH,
    cap_detail_response,
    cap_list_response,
    caps_response,
    cap_mutation_response,
    data_sse,
    event_item,
    fallback_agent_snapshot,
    message_item,
    prompt_detail_item,
    prompt_item,
    psyche_detail_item,
    psyche_item,
    runtime_run_item,
    service_detail_item,
    service_item,
    skill_detail_item,
    skill_item,
    sse,
    step_item,
    thread_item,
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
    room = AgentHome.resolve(prepared.ref.home).room(prepared.ref.name)
    runtime_process = RuntimeProcess(
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
        runtime_process.start()
        try:
            yield
        finally:
            runtime_process.stop()

    app = FastAPI(
        title=f"Toolang Agent API: {prepared.ref.name}",
        lifespan=lifespan,
    )
    add_cors(app, allow_origins=cors_allow_origins)

    def _run_messages(run) -> list[AgentChatMessage]:
        return [message_item(item) for item in runtime_process.execution.messages_for_run(run_id=run.run_id)]

    def _thread_preview_and_channel(thread_id: str) -> tuple[str | None, str | None]:
        return runtime_process.execution.thread_preview(thread_id=thread_id)

    @app.middleware("http")
    async def update_heartbeat(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        response = await call_next(request)
        runtime_process.touch()
        return response

    @app.exception_handler(ExternalDependencyUnavailableError)
    async def handle_dependency_unavailable(
        _: Request,
        exc: ExternalDependencyUnavailableError,
    ) -> JSONResponse:
        return JSONResponse(status_code=503, content={"detail": str(exc)})

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
            "endpoint": runtime_process.endpoint,
        }

    @app.get("/api/v1/agent")
    @app.get("/agent", response_model_exclude_none=True)
    def agent_info():
        snapshot = runtime_process.bus.get_agent(prepared.ref.uri)
        if snapshot is not None:
            return snapshot
        return fallback_agent_snapshot(
            prepared,
            endpoint=runtime_process.endpoint,
            sandbox=runtime_process.sandbox,
            now=utc_now(),
        )

    @app.get("/api/v1/profile", response_model=AgentProfile)
    def profile() -> AgentProfile:
        return AgentProfile(agent=prepared.ref.name)

    @app.get("/api/v1/runtime", response_model=AgentRuntimeResponse)
    def runtime_info() -> AgentRuntimeResponse:
        current_run = get_running_agent(agents_db_path, prepared.ref.uri)
        current = runtime_process.current_prepared()
        return AgentRuntimeResponse(
            status="online",
            checked_at=utc_now(),
            activation_id=runtime_process.activation_id,
            endpoint=runtime_process.endpoint,
            execution_host=SandboxSpec.parse(runtime_process.sandbox).execution_host,
            working_directory=str(prepared.ref.home),
            sandbox=runtime_process.sandbox,
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
                runtime_security_snapshot(
                    prepared=current,
                    room=room,
                    sandbox=runtime_process.sandbox,
                    runtime_loops=runtime_process.runtime_loops,
                )
            ),
        )

    @app.get("/api/v1/caps", response_model=AgentCapsResponse)
    def list_caps() -> AgentCapsResponse:
        current = runtime_process.current_prepared()
        caps = load_prepared_caps(current)
        return caps_response(current, caps)

    @app.get("/api/v1/psyches", response_model=CapListResponse)
    def list_psyches() -> CapListResponse:
        return _list_cap_collection("psyche")

    @app.get("/api/v1/prompts", response_model=CapListResponse)
    def list_prompts() -> CapListResponse:
        return _list_cap_collection("prompt")

    @app.get("/api/v1/services", response_model=CapListResponse)
    def list_services() -> CapListResponse:
        return _list_cap_collection("service")

    @app.get("/api/v1/skills", response_model=CapListResponse)
    def list_skills() -> CapListResponse:
        return _list_cap_collection("skill")

    @app.get("/api/v1/psyches/{cap_name:path}", response_model=CapDetailResponse)
    def get_psyche(cap_name: str) -> CapDetailResponse:
        return _get_cap_detail("psyche", cap_name)

    @app.get("/api/v1/prompts/{cap_name:path}", response_model=CapDetailResponse)
    def get_prompt(cap_name: str) -> CapDetailResponse:
        return _get_cap_detail("prompt", cap_name)

    @app.get("/api/v1/services/{cap_name:path}", response_model=CapDetailResponse)
    def get_service(cap_name: str) -> CapDetailResponse:
        return _get_cap_detail("service", cap_name)

    @app.get("/api/v1/skills/{cap_name:path}", response_model=CapDetailResponse)
    def get_skill(cap_name: str) -> CapDetailResponse:
        return _get_cap_detail("skill", cap_name)

    @app.put("/api/v1/caps/{cap_kind}/{cap_name:path}", response_model=CapMutationResponse)
    def put_cap(
        cap_kind: str,
        cap_name: str,
        request: CapPutRequest,
    ) -> CapMutationResponse:
        return _put_cap(_cap_kind(cap_kind), cap_name, request)

    @app.put("/api/v1/psyches/{cap_name:path}", response_model=CapMutationResponse)
    def put_psyche(cap_name: str, request: CapPutRequest) -> CapMutationResponse:
        return _put_cap("psyche", cap_name, request)

    @app.put("/api/v1/prompts/{cap_name:path}", response_model=CapMutationResponse)
    def put_prompt(cap_name: str, request: CapPutRequest) -> CapMutationResponse:
        return _put_cap("prompt", cap_name, request)

    @app.put("/api/v1/services/{cap_name:path}", response_model=CapMutationResponse)
    def put_service(cap_name: str, request: CapPutRequest) -> CapMutationResponse:
        return _put_cap("service", cap_name, request)

    @app.put("/api/v1/skills/{cap_name:path}", response_model=CapMutationResponse)
    def put_skill(cap_name: str, request: CapPutRequest) -> CapMutationResponse:
        return _put_cap("skill", cap_name, request)

    @app.delete("/api/v1/caps/{cap_kind}/{cap_name:path}", response_model=CapDeleteResponse)
    def delete_cap(
        cap_kind: str,
        cap_name: str,
        scope: str = Query(...),
        source: str | None = Query(None),
    ) -> CapDeleteResponse:
        return _delete_cap(_cap_kind(cap_kind), cap_name, scope=scope, source=source)

    @app.delete("/api/v1/psyches/{cap_name:path}", response_model=CapDeleteResponse)
    def delete_psyche(
        cap_name: str,
        scope: str = Query(...),
        source: str | None = Query(None),
    ) -> CapDeleteResponse:
        return _delete_cap("psyche", cap_name, scope=scope, source=source)

    @app.delete("/api/v1/prompts/{cap_name:path}", response_model=CapDeleteResponse)
    def delete_prompt(
        cap_name: str,
        scope: str = Query(...),
        source: str | None = Query(None),
    ) -> CapDeleteResponse:
        return _delete_cap("prompt", cap_name, scope=scope, source=source)

    @app.delete("/api/v1/services/{cap_name:path}", response_model=CapDeleteResponse)
    def delete_service(
        cap_name: str,
        scope: str = Query(...),
        source: str | None = Query(None),
    ) -> CapDeleteResponse:
        return _delete_cap("service", cap_name, scope=scope, source=source)

    @app.delete("/api/v1/skills/{cap_name:path}", response_model=CapDeleteResponse)
    def delete_skill(
        cap_name: str,
        scope: str = Query(...),
        source: str | None = Query(None),
    ) -> CapDeleteResponse:
        return _delete_cap("skill", cap_name, scope=scope, source=source)

    def _put_cap(
        kind: CapKind,
        cap_name: str,
        request: CapPutRequest,
    ) -> CapMutationResponse:
        current = runtime_process.current_prepared()
        result = control_put_cap(
            runtime_process.bus,
            current.ref,
            kind=kind,
            name=cap_name,
            scope=request.scope,
            source=request.source,
            ref=request.ref,
            content=request.content,
        )
        return cap_mutation_response(result)

    def _delete_cap(
        kind: CapKind,
        cap_name: str,
        *,
        scope: str,
        source: str | None,
    ) -> CapDeleteResponse:
        current = runtime_process.current_prepared()
        control_delete_cap(
            runtime_process.bus,
            current.ref,
            kind=kind,
            name=cap_name,
            scope=scope,
            source=source,
        )
        return CapDeleteResponse()

    @app.get("/api/v1/tasks", response_model=TaskListResponse)
    def list_tasks() -> TaskListResponse:
        return TaskListResponse(items=list_task_items(room))

    @app.put("/api/v1/tasks/{task_name:path}", response_model=TaskItem)
    def put_task(task_name: str, request: TaskPutRequest) -> TaskItem:
        return put_task_item(room, task_name, request)

    @app.patch("/api/v1/tasks/{task_name:path}", response_model=TaskItem)
    def patch_task(task_name: str, request: TaskPatchRequest) -> TaskItem:
        return patch_task_item(room, task_name, request)

    @app.get("/api/v1/chores", response_model=ChoreListResponse)
    def list_chores() -> ChoreListResponse:
        return ChoreListResponse(items=list_chore_items(room))

    @app.put("/api/v1/chores/{chore_id:path}", response_model=ChoreItem)
    def put_chore(chore_id: str, request: ChorePutRequest) -> ChoreItem:
        return put_chore_item(room, chore_id, request)

    @app.patch("/api/v1/chores/{chore_id:path}", response_model=ChoreItem)
    def patch_chore(chore_id: str, request: ChorePatchRequest) -> ChoreItem:
        return patch_chore_item(room, chore_id, request)

    @app.get("/api/v1/will", response_model=WillResponse)
    def get_will() -> WillResponse:
        return WillResponse(item=load_will_item(room, agent=prepared.ref))

    @app.put("/api/v1/will", response_model=WillResponse)
    def put_will(request: WillPutRequest) -> WillResponse:
        return WillResponse(item=put_will_item(room, request, agent=prepared.ref))

    @app.patch("/api/v1/will", response_model=WillResponse)
    def patch_will(request: WillPatchRequest) -> WillResponse:
        return WillResponse(item=patch_will_item(room, request, agent=prepared.ref))

    def _list_threads(*, kind: str | None, limit: int) -> ThreadListResponse:
        threads = runtime_process.execution.list_threads(
            agent_uri=prepared.ref.uri,
            limit=limit,
        )
        if kind is not None:
            threads = [item for item in threads if item.thread_group == kind]
        items = []
        for thread in threads:
            preview, channel = _thread_preview_and_channel(thread.thread_id)
            items.append(
                thread_item(
                    thread,
                    preview=preview,
                    channel=channel,
                )
            )
        return ThreadListResponse(items=items)

    def _get_thread_response(thread_id: str, *, limit: int) -> ThreadResponse:
        thread = runtime_process.execution.get_thread(thread_id=thread_id)
        if thread is None or thread.agent_uri != prepared.ref.uri:
            raise HTTPException(status_code=404, detail="thread not found")
        preview, channel = _thread_preview_and_channel(thread_id)
        runs = runtime_process.execution.list_runs(
            thread_id=thread_id,
            limit=limit,
        )
        return ThreadResponse(
            thread=thread_item(
                thread,
                preview=preview,
                channel=channel,
            ),
            runs=[runtime_run_item(item) for item in runs],
            messages=[item for run in reversed(runs) for item in _run_messages(run)],
        )

    def _list_cap_collection(kind: CapKind) -> CapListResponse:
        current = runtime_process.current_prepared()
        caps = load_prepared_caps(current)
        return cap_list_response(_cap_items_for_kind(current, caps, kind))

    def _get_cap_detail(kind: CapKind, cap_name: str) -> CapDetailResponse:
        current = runtime_process.current_prepared()
        caps = load_prepared_caps(current)
        item = _cap_detail_for_kind(current, caps, kind, cap_name)
        if item is None:
            raise HTTPException(status_code=404, detail="cap not found")
        return cap_detail_response(item)

    def _cap_items_for_kind(
        current: PreparedAgent,
        caps,
        kind: CapKind,
    ) -> list:
        if kind == "skill":
            return [skill_item(item, agent_kind=current.ref.kind) for item in caps.skills]
        if kind == "service":
            return [service_item(item, agent_kind=current.ref.kind) for item in caps.services]
        if kind == "prompt":
            return [prompt_item(item, agent_kind=current.ref.kind) for item in caps.prompts]
        return [psyche_item(item, agent_kind=current.ref.kind) for item in caps.psyches]

    def _cap_detail_for_kind(
        current: PreparedAgent,
        caps,
        kind: CapKind,
        cap_name: str,
    ):
        if kind == "skill":
            for item in caps.skills:
                if item.name == cap_name:
                    return skill_detail_item(item, agent_kind=current.ref.kind)
            return None
        if kind == "service":
            for item in caps.services:
                if item.name == cap_name:
                    return service_detail_item(item, agent_kind=current.ref.kind)
            return None
        if kind == "prompt":
            for item in caps.prompts:
                if item.name == cap_name:
                    return prompt_detail_item(item, agent_kind=current.ref.kind)
            return None
        for item in caps.psyches:
            if item.name == cap_name:
                return psyche_detail_item(item, agent_kind=current.ref.kind)
        return None

    def _cap_kind(segment: str) -> CapKind:
        normalized = segment.strip().lower()
        if normalized == "services":
            return "service"
        if normalized == "prompts":
            return "prompt"
        if normalized == "skills":
            return "skill"
        if normalized == "psyches":
            return "psyche"
        if normalized in {"service", "prompt", "skill", "psyche"}:
            return normalized  # type: ignore[return-value]
        raise HTTPException(status_code=404, detail="cap kind not found")

    @app.get("/api/v1/threads", response_model=ThreadListResponse)
    def list_threads(
        kind: str | None = Query(None),
        limit: int = Query(50, ge=1, le=500),
    ) -> ThreadListResponse:
        return _list_threads(kind=kind, limit=limit)

    @app.get("/api/v1/chats", response_model=ThreadListResponse)
    def list_chats(limit: int = Query(50, ge=1, le=500)) -> ThreadListResponse:
        return _list_threads(kind="chat", limit=limit)

    @app.get("/api/v1/threads/{thread_id:path}", response_model=ThreadResponse)
    @app.get("/api/v1/chats/{thread_id:path}", response_model=ThreadResponse)
    def get_thread(
        thread_id: str, limit: int = Query(50, ge=1, le=500)
    ) -> ThreadResponse:
        return _get_thread_response(thread_id, limit=limit)

    @app.get("/api/v1/runs", response_model=RunListResponse)
    def list_runs(
        origin: MessageOrigin | None = Query(None),
        thread_id: str | None = Query(None),
        status: RunStatus | None = Query(None),
        limit: int = Query(50, ge=1, le=500),
    ) -> RunListResponse:
        runs = runtime_process.execution.list_runs(
            origin=origin,
            thread_id=thread_id,
            status=status,
            limit=limit,
        )
        return RunListResponse(items=[runtime_run_item(item) for item in runs])

    @app.get("/api/v1/runs/{run_id}", response_model=RunDetailResponse)
    def get_run(run_id: str) -> RunDetailResponse:
        run = runtime_process.execution.get_run(run_id=run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")
        events = runtime_process.bus.list_events(
            agent_uri=prepared.ref.uri,
            run_id=run_id,
            limit=500,
        )
        return RunDetailResponse(
            run=runtime_run_item(run),
            steps=[step_item(item) for item in runtime_process.execution.list_steps(run_id=run_id)],
            events=[event_item(item) for item in events],
            messages=_run_messages(run),
        )

    @app.get("/api/v1/runs/{run_id}/prompt", response_model=PromptTraceItem)
    def get_run_prompt(run_id: str) -> PromptTraceItem:
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
        snapshot = runtime_process.bus.get_agent(prepared.ref.uri)
        if snapshot is not None and snapshot.created_event_id is not None:
            from_event_id = max(from_event_id, snapshot.created_event_id - 1)
        events = runtime_process.bus.list_events(
            agent_uri=prepared.ref.uri,
            from_event_id=from_event_id,
            limit=limit,
        )
        return EventListResponse(items=[event_item(item) for item in events])

    @app.get("/api/v1/events/stream")
    async def stream_events(request: Request) -> StreamingResponse:
        async def stream() -> AsyncIterator[str]:
            min_event_id = await asyncio.to_thread(
                runtime_process.bus.max_event_id,
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
                    runtime_process.bus.list_events,
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
            runtime_diagnostics_snapshot(
                prepared=runtime_process.current_prepared(refresh=False),
                room=room,
                sandbox=runtime_process.sandbox,
                runtime_loops=runtime_process.runtime_loops,
                channels_config=runtime_process.channels_config,
                channel_plugins=runtime_process.channel_plugins,
                scheduler_snapshot=runtime_process.scheduler.snapshot(),
                pulse_pending=runtime_process.pulse_pending_keys(),
            )
        )

    @app.post("/api/v1/chat", response_model=ChatResponse)
    def chat(request: ChatRequest) -> ChatResponse:
        assistant = runtime_process.submit_chat(request).assistant
        item = message_item(assistant)
        return ChatResponse(
            thread_id=assistant.thread_id,
            run_id=assistant.run_id,
            message=item,
            assistant=item,
        )

    @app.post("/api/v1/chat/stream")
    def chat_stream(request: ChatRequest) -> StreamingResponse:
        def stream() -> Iterator[str]:
            event_queue: Queue[object] = Queue()
            try:
                run_id, future = runtime_process.submit_chat_stream(
                    request,
                    event_queue.put,
                )
                encoder = AIMessageChunkEncoder(message_id=run_id)
                for chunk in encoder.start():
                    yield data_sse(chunk_to_dict(chunk))
                while True:
                    try:
                        event = event_queue.get(timeout=0.05)
                    except Empty:
                        if future.done():
                            break
                        continue
                    for chunk in encoder.encode_event(event):
                        yield data_sse(chunk_to_dict(chunk))
                assistant = future.result().assistant
            except Exception as exc:
                message = str(exc).strip() or type(exc).__name__
                encoder = locals().get("encoder")
                if isinstance(encoder, AIMessageChunkEncoder):
                    for chunk in encoder.error(message):
                        yield data_sse(chunk_to_dict(chunk))
                else:
                    yield data_sse({"type": "error", "errorText": message})
                    yield data_sse({"type": "finish"})
                yield "data: [DONE]\n\n"
                return

            if assistant.text and not encoder.has_text:
                for chunk in encoder.encode_event(TextDeltaEvent(delta=assistant.text)):
                    yield data_sse(chunk_to_dict(chunk))
            for chunk in encoder.finish():
                yield data_sse(chunk_to_dict(chunk))
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
            result = runtime_process.submit_run(request)
        except ToolangError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return RunResponse(run_id=result.run_id, output=result.output)

    return app


def _default_model(prepared: PreparedAgent) -> str | None:
    try:
        return infer_model(prepared.program.default_thunk())
    except ToolangError:
        return None
