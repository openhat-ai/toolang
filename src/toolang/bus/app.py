from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator
from urllib.parse import urlsplit

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from toolang.runtime.api_models import (
    AgentListResponse,
    BusAgentItem,
    ChatRequest,
    EventItem,
    EventListResponse,
    RunItem,
    RunListResponse,
)
from toolang.bus.db import AgentSnapshot, BusStore, RunSnapshot, StoredEvent
from toolang.web import add_cors

SSE_POLL_INTERVAL_SEC = 0.5
SSE_PING_INTERVAL_SEC = 20.0


def serve_bus_app(
    db_path: Path,
    *,
    host: str,
    port: int,
    cors_allow_origins: list[str] | None = None,
) -> None:
    uvicorn.run(
        create_bus_app(db_path, cors_allow_origins=cors_allow_origins),
        host=host,
        port=port,
        log_level="info",
    )


def create_bus_app(db_path: Path, *, cors_allow_origins: list[str] | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.store = BusStore(db_path)
        try:
            yield
        finally:
            app.state.store.close()

    app = FastAPI(title="Toolang Bus API", lifespan=lifespan)
    add_cors(app, allow_origins=cors_allow_origins)

    @app.get("/healthz")
    def healthz() -> dict[str, object]:
        return {"ok": True}

    @app.get("/api/v1/agents", response_model=AgentListResponse)
    def list_agents(limit: int = Query(200, ge=1, le=2000)) -> AgentListResponse:
        store: BusStore = app.state.store
        return AgentListResponse(items=[_agent_item(item) for item in store.list_agents(limit=limit)])

    @app.get("/api/v1/agents/{agent_id}", response_model=BusAgentItem)
    def get_agent(agent_id: str) -> BusAgentItem:
        store: BusStore = app.state.store
        return _agent_item(_agent_snapshot_or_404(store, agent_id))

    @app.post("/api/v1/agents/{agent_id}/chat")
    async def proxy_chat(agent_id: str, req: ChatRequest) -> object:
        snapshot = _agent_snapshot_or_404(app.state.store, agent_id)
        endpoint = _require_endpoint(snapshot)
        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(_agent_api_url(endpoint, "/api/v1/chat"), json=req.model_dump())
            except httpx.HTTPError as exc:
                raise HTTPException(status_code=502, detail=str(exc) or type(exc).__name__) from exc
        if response.status_code >= 400:
            detail = _upstream_detail(response)
            raise HTTPException(status_code=response.status_code, detail=detail)
        return response.json()

    @app.post("/api/v1/agents/{agent_id}/chat/stream")
    async def proxy_chat_stream(agent_id: str, req: ChatRequest) -> StreamingResponse:
        snapshot = _agent_snapshot_or_404(app.state.store, agent_id)
        endpoint = _require_endpoint(snapshot)
        url = _agent_api_url(endpoint, "/api/v1/chat/stream")

        async def stream() -> AsyncIterator[bytes]:
            try:
                async with httpx.AsyncClient(timeout=None) as client:
                    async with client.stream("POST", url, json=req.model_dump()) as response:
                        if response.status_code >= 400:
                            detail = await response.aread()
                            message = detail.decode("utf-8", errors="replace").strip()
                            yield _sse(
                                "error",
                                {
                                    "error": {
                                        "type": "UpstreamHTTPError",
                                        "message": message or f"Upstream returned {response.status_code}",
                                        "status_code": response.status_code,
                                    },
                                    "agent_id": agent_id,
                                    "thread_id": req.thread,
                                },
                            ).encode("utf-8")
                            yield b"data: [DONE]\n\n"
                            return
                        async for chunk in response.aiter_bytes():
                            yield chunk
            except httpx.HTTPError as exc:
                yield _sse(
                    "error",
                    {
                        "error": {
                            "type": type(exc).__name__,
                            "message": str(exc) or type(exc).__name__,
                            "status_code": 502,
                        },
                        "agent_id": agent_id,
                        "thread_id": req.thread,
                    },
                ).encode("utf-8")
                yield b"data: [DONE]\n\n"

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @app.get("/api/v1/runs", response_model=RunListResponse)
    def list_runs(
        agent_id: str | None = None,
        limit: int = Query(200, ge=1, le=2000),
    ) -> RunListResponse:
        store: BusStore = app.state.store
        agent_uri = _agent_snapshot_or_404(store, agent_id).agent_uri if agent_id is not None else None
        return RunListResponse(
            items=[_run_item(item) for item in store.list_runs(agent_uri=agent_uri, limit=limit)]
        )

    @app.get("/api/v1/events", response_model=EventListResponse)
    def list_events(
        from_event_id: int = Query(0, ge=0),
        to_event_id: int | None = Query(None, ge=0),
        limit: int = Query(200, ge=1, le=2000),
    ) -> EventListResponse:
        store: BusStore = app.state.store
        return EventListResponse(
            items=[
                _event_item(item)
                for item in store.list_events(
                    from_event_id=from_event_id,
                    to_event_id=to_event_id,
                    limit=limit,
                )
            ]
        )

    @app.get("/api/v1/agents/{agent_id}/events", response_model=EventListResponse)
    def list_agent_events(
        agent_id: str,
        from_event_id: int = Query(0, ge=0),
        to_event_id: int | None = Query(None, ge=0),
        limit: int = Query(200, ge=1, le=2000),
    ) -> EventListResponse:
        store: BusStore = app.state.store
        snapshot = _agent_snapshot_or_404(store, agent_id)
        return EventListResponse(
            items=[
                _event_item(item)
                for item in store.list_events(
                    agent_uri=snapshot.agent_uri,
                    from_event_id=from_event_id,
                    to_event_id=to_event_id,
                    limit=limit,
                )
            ]
        )

    @app.get("/api/v1/events/stream")
    async def stream_events(request: Request) -> StreamingResponse:
        store: BusStore = app.state.store

        async def stream() -> AsyncIterator[str]:
            min_event_id = await asyncio.to_thread(store.max_event_id)
            last_emitted = min_event_id
            last_ping_at = asyncio.get_running_loop().time()
            yield _sse("sub_ready", {"min_event_id": min_event_id})
            while True:
                if await request.is_disconnected():
                    break
                rows = await asyncio.to_thread(
                    store.list_events,
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

    @app.get("/api/v1/agents/{agent_id}/events/stream")
    async def stream_agent_events(agent_id: str, request: Request) -> StreamingResponse:
        store: BusStore = app.state.store
        snapshot = _agent_snapshot_or_404(store, agent_id)

        async def stream() -> AsyncIterator[str]:
            min_event_id = await asyncio.to_thread(store.max_event_id, agent_uri=snapshot.agent_uri)
            last_emitted = min_event_id
            last_ping_at = asyncio.get_running_loop().time()
            yield _sse("sub_ready", {"min_event_id": min_event_id, "agent_id": agent_id})
            while True:
                if await request.is_disconnected():
                    break
                rows = await asyncio.to_thread(
                    store.list_events,
                    agent_uri=snapshot.agent_uri,
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
                    yield _sse("ping", {"agent_id": agent_id})
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

    return app


def _agent_snapshot_or_404(store: BusStore, agent_id: str) -> AgentSnapshot:
    snapshot = store.get_agent_by_id(agent_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="agent not found")
    return snapshot


def _require_endpoint(snapshot: AgentSnapshot) -> str:
    endpoint = snapshot.endpoint
    if endpoint is None or not endpoint.strip():
        raise HTTPException(status_code=409, detail="agent endpoint unavailable")
    return endpoint


def _agent_api_url(endpoint: str, path: str) -> str:
    return endpoint.rstrip("/") + path


def _upstream_detail(response: httpx.Response) -> object:
    try:
        return response.json()
    except Exception:
        return response.text


def _agent_item(snapshot: AgentSnapshot) -> BusAgentItem:
    host: str | None = None
    port: int | None = None
    if snapshot.endpoint:
        parsed = urlsplit(snapshot.endpoint)
        host = parsed.hostname
        port = parsed.port
    return BusAgentItem(
        id=snapshot.agent_id,
        name=snapshot.name,
        status=snapshot.status,
        endpoint=snapshot.endpoint,
        model=None,
        host=host,
        port=port,
        sandbox=snapshot.sandbox,
        runtime_ref=snapshot.agent_uri,
        detail=snapshot.detail,
        created_at=snapshot.created_at,
        updated_at=snapshot.updated_at,
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


def _event_item(item: StoredEvent) -> EventItem:
    return EventItem(
        event_id=item.event_id,
        event_type=item.event_type,
        at=item.at,
        agent_id=item.agent_id,
        run_id=item.run_id,
        payload=dict(item.payload),
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


def _sse(event: str, data: dict[str, object], event_id: int | None = None) -> str:
    lines: list[str] = []
    if event_id is not None:
        lines.append(f"id: {event_id}")
    lines.append(f"event: {event}")
    lines.append("data: " + json.dumps(data, ensure_ascii=False, separators=(",", ":")))
    return "\n".join(lines) + "\n\n"
