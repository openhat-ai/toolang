from __future__ import annotations

import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable

import uvicorn
from fastapi import FastAPI, HTTPException, Request, Response
from pydantic import BaseModel

from toolang.agent_registry import (
    KnownAgentRecord,
    RunningAgentRecord,
    delete_running_agent,
    get_running_agent,
    upsert_known_agent,
    upsert_running_agent,
)
from toolang.errors import ToolangError
from toolang.files.agent_run import AgentRunState
from toolang.layout import agent_run_path
from toolang.prepared import PreparedAgent, prepare_agent
from toolang.runtime import execute_thunk

SHORT_AGENT_ID_LENGTH = 12


class RunRequest(BaseModel):
    thunk: str | None = None
    input: str | None = None
    model: str | None = None


class RunResponse(BaseModel):
    output: str


def serve_agent(
    prepared: PreparedAgent,
    *,
    agents_db_path: Path,
    host: str,
    port: int,
) -> None:
    endpoint = f"http://{host}:{port}"
    app = create_agent_app(
        prepared,
        agents_db_path=agents_db_path,
        host=host,
        port=port,
    )
    try:
        uvicorn.run(app, host=host, port=port, log_level="info")
    finally:
        if _has_running_state(prepared, agents_db_path=agents_db_path):
            _deactivate_running_agent(prepared, agents_db_path=agents_db_path, endpoint=endpoint)


def create_agent_app(
    prepared: PreparedAgent,
    *,
    agents_db_path: Path,
    host: str,
    port: int,
) -> FastAPI:
    endpoint = f"http://{host}:{port}"

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        _activate_running_agent(prepared, agents_db_path=agents_db_path, endpoint=endpoint)
        try:
            yield
        finally:
            _deactivate_running_agent(prepared, agents_db_path=agents_db_path, endpoint=endpoint)

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

    @app.get("/health")
    def health() -> dict[str, str]:
        return {
            "status": "ok",
            "agent_uri": prepared.ref.agent_uri,
            "agent_id": prepared.ref.agent_id[:SHORT_AGENT_ID_LENGTH],
            "agent_name": prepared.ref.agent_name,
            "endpoint": endpoint,
        }

    @app.get("/agent")
    def agent_info() -> dict[str, str]:
        return {
            "agent_uri": prepared.ref.agent_uri,
            "agent_id": prepared.ref.agent_id[:SHORT_AGENT_ID_LENGTH],
            "agent_name": prepared.ref.agent_name,
            "agent_home": str(prepared.ref.agent_home),
            "source_file": prepared.source_path.name,
            "endpoint": endpoint,
        }

    @app.post("/runs", response_model=RunResponse)
    def run_thunk(request: RunRequest) -> RunResponse:
        current = prepare_agent(prepared.ref)
        try:
            selected_thunk = current.program.get_thunk(request.thunk)
            output = execute_thunk(
                current.program,
                selected_thunk,
                current.source_path,
                user_input=request.input,
                model=request.model,
            )
        except ToolangError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return RunResponse(output=output)

    return app


def _activate_running_agent(
    prepared: PreparedAgent,
    *,
    agents_db_path: Path,
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
    endpoint: str,
) -> None:
    current = get_running_agent(agents_db_path, prepared.ref.agent_uri)
    now = datetime.now(timezone.utc)
    started_at = current.started_at if current is not None else now
    delete_running_agent(agents_db_path, prepared.ref.agent_uri)
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
    return agent_run_path(prepared.ref.agent_home, prepared.ref.agent_name).exists()


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True
