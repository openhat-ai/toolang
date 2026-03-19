from __future__ import annotations

import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable

import uvicorn
from fastapi import FastAPI, HTTPException, Request, Response
from pydantic import BaseModel

from toolang.bus.db import AgentSnapshot, BusStore, RunSnapshot, StoredEvent
from toolang.bus.events import AgentStarted, AgentStopped, utc_now
from toolang.agent_registry import (
    KnownAgentRecord,
    RunningAgentRecord,
    delete_running_agent,
    get_running_agent,
    upsert_known_agent,
    upsert_running_agent,
)
from toolang.caps_view import CapsView, load_prepared_caps
from toolang.errors import ToolangError
from toolang.files.agent_run import AgentRunState
from toolang.layout import agent_run_path
from toolang.invoke import invoke_prepared_agent
from toolang.prepared import PreparedAgent, prepare_agent

SHORT_AGENT_ID_LENGTH = 12


class RunRequest(BaseModel):
    thunk: str | None = None
    input: str | None = None
    model: str | None = None


class RunResponse(BaseModel):
    run_id: str
    output: str


def serve_agent(
    prepared: PreparedAgent,
    *,
    agents_db_path: Path,
    bus_db_path: Path,
    host: str,
    port: int,
) -> None:
    endpoint = f"http://{host}:{port}"
    app = create_agent_app(
        prepared,
        agents_db_path=agents_db_path,
        bus_db_path=bus_db_path,
        host=host,
        port=port,
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
) -> FastAPI:
    endpoint = f"http://{host}:{port}"
    bus = BusStore(bus_db_path)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
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
                bus.close()

    app = FastAPI(
        title=f"Toolang Agent Server: {prepared.ref.agent_name}",
        lifespan=lifespan,
    )

    @app.middleware("http")
    async def update_heartbeat(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        response = await call_next(request)
        _touch_running_agent(prepared, agents_db_path=agents_db_path, endpoint=endpoint)
        return response

    def build_health() -> dict[str, str]:
        return {
            "status": "ok",
            "agent_uri": prepared.ref.agent_uri,
            "agent_id": prepared.ref.agent_id[:SHORT_AGENT_ID_LENGTH],
            "agent_name": prepared.ref.agent_name,
            "endpoint": endpoint,
        }

    @app.get("/api/v1/health")
    @app.get("/health")
    def health() -> dict[str, str]:
        return build_health()

    @app.get("/api/v1/agent")
    @app.get("/agent", response_model_exclude_none=True)
    def agent_info() -> AgentSnapshot:
        snapshot = bus.get_agent(prepared.ref.agent_uri)
        if snapshot is not None:
            return snapshot
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
            created_at=utc_now(),
            updated_at=utc_now(),
        )

    @app.get("/api/v1/caps")
    def list_caps() -> CapsView:
        current = prepare_agent(prepared.ref)
        return load_prepared_caps(current)

    @app.get("/api/v1/runs")
    def list_runs(limit: int = 50) -> list[RunSnapshot]:
        return bus.list_runs(agent_uri=prepared.ref.agent_uri, limit=limit)

    @app.get("/api/v1/events")
    def list_events(from_event_id: int = 0, limit: int = 100) -> list[StoredEvent]:
        return bus.list_events(
            agent_uri=prepared.ref.agent_uri,
            from_event_id=from_event_id,
            limit=limit,
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
    _write_agent_run_state(prepared, endpoint=endpoint, status="running", started_at=now, heartbeat_at=now)


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
