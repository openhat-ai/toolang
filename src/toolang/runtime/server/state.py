from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

from toolang.agent.prepared import PreparedAgent
from toolang.agent.registry import (
    KnownAgentRecord,
    RunningAgentRecord,
    delete_running_agent,
    get_running_agent,
    upsert_known_agent,
    upsert_running_agent,
)
from toolang.bus.db import BusStore
from toolang.bus.events import AgentStarted, AgentStopped, utc_now
from toolang.concepts.layout import AgentHome
from toolang.concepts.sandbox import SandboxSpec, SandboxState
from toolang.errors import ToolangError
from toolang.concepts.persisted.activation_state import ActivationState
from toolang.sandbox import sandbox_alive

SHORT_AGENT_ID_LENGTH = 12


def activate_running_agent(
    prepared: PreparedAgent,
    *,
    agents_db_path: Path,
    bus: BusStore,
    endpoint: str,
    sandbox: str,
) -> None:
    current_pid = os.getpid()
    parsed_sandbox = SandboxSpec.parse(sandbox)
    sandbox_spec = parsed_sandbox.spec
    existing = get_running_agent(agents_db_path, prepared.ref.uri)
    if existing is not None:
        alive = sandbox_alive(
            SandboxState.for_spec(
                SandboxSpec.parse(existing.sandbox),
                agent_name=prepared.ref.name,
                agent_id=prepared.ref.id[:SHORT_AGENT_ID_LENGTH],
                pid=existing.pid,
            )
        )
        if alive and existing.pid != current_pid:
            raise ToolangError(f"Agent is already being served: {prepared.ref.uri}")
        if not alive:
            delete_running_agent(agents_db_path, prepared.ref.uri)

    now = datetime.now(timezone.utc)
    upsert_known_agent(
        agents_db_path,
        KnownAgentRecord.from_agent(prepared.ref, updated_at=now),
    )
    upsert_running_agent(
        agents_db_path,
        RunningAgentRecord(
            agent_uri=prepared.ref.uri,
            pid=current_pid,
            status="running",
            endpoint=endpoint,
            sandbox=sandbox_spec,
            started_at=now,
            heartbeat_at=now,
        ),
    )
    bus.append(
        AgentStarted(
            at=utc_now(),
            agent_uri=prepared.ref.uri,
            agent_id=prepared.ref.id[:SHORT_AGENT_ID_LENGTH],
            name=prepared.ref.name,
            kind=prepared.ref.kind,
            sandbox=sandbox_spec,
            endpoint=endpoint,
            agent_home=str(prepared.ref.home),
            source_file=prepared.ref.source.name,
        )
    )
    write_agent_run_state(
        prepared,
        endpoint=endpoint,
        status="running",
        started_at=now,
        heartbeat_at=now,
        sandbox=sandbox_spec,
    )


def touch_running_agent(
    prepared: PreparedAgent,
    *,
    agents_db_path: Path,
    endpoint: str,
) -> None:
    current = get_running_agent(agents_db_path, prepared.ref.uri)
    if current is None:
        return
    now = datetime.now(timezone.utc)
    updated = current.model_copy(update={"heartbeat_at": now})
    upsert_running_agent(agents_db_path, updated)
    write_agent_run_state(
        prepared,
        endpoint=endpoint,
        status=updated.status,
        started_at=updated.started_at,
        heartbeat_at=now,
        sandbox=updated.sandbox,
    )


def deactivate_running_agent(
    prepared: PreparedAgent,
    *,
    agents_db_path: Path,
    bus: BusStore,
    endpoint: str,
    sandbox: str,
) -> None:
    current = get_running_agent(agents_db_path, prepared.ref.uri)
    now = datetime.now(timezone.utc)
    started_at = current.started_at if current is not None else now
    delete_running_agent(agents_db_path, prepared.ref.uri)
    sandbox_spec = SandboxSpec.parse(sandbox).spec
    bus.append(
        AgentStopped(
            at=utc_now(),
            agent_uri=prepared.ref.uri,
            agent_id=prepared.ref.id[:SHORT_AGENT_ID_LENGTH],
            name=prepared.ref.name,
            sandbox=sandbox_spec,
            detail="server stopped",
            endpoint=endpoint,
            agent_home=str(prepared.ref.home),
            source_file=prepared.ref.source.name,
        )
    )
    write_agent_run_state(
        prepared,
        endpoint=endpoint,
        status="stopped",
        started_at=started_at,
        heartbeat_at=now,
        sandbox=sandbox_spec,
    )


def write_agent_run_state(
    prepared: PreparedAgent,
    *,
    endpoint: str,
    status: str,
    started_at: datetime,
    heartbeat_at: datetime,
    sandbox: str,
) -> None:
    parsed_sandbox = SandboxSpec.parse(sandbox)
    run_path = AgentHome.resolve(prepared.ref.home).room(prepared.ref.name).run_path
    run_path.parent.mkdir(parents=True, exist_ok=True)
    ActivationState(
        agent_uri=prepared.ref.uri,
        agent_id=prepared.ref.id[:SHORT_AGENT_ID_LENGTH],
        agent_name=prepared.ref.name,
        agent_home=str(prepared.ref.home),
        source_file=prepared.ref.source.name,
        pid=os.getpid(),
        status=status,
        endpoint=endpoint,
        started_at=started_at,
        heartbeat_at=heartbeat_at,
        sandbox=SandboxState.for_spec(
            parsed_sandbox,
            agent_name=prepared.ref.name,
            agent_id=prepared.ref.id[:SHORT_AGENT_ID_LENGTH],
            pid=os.getpid(),
            port=port_from_endpoint(endpoint),
        ),
    ).save(run_path)


def has_running_state(
    prepared: PreparedAgent,
    *,
    agents_db_path: Path,
) -> bool:
    if get_running_agent(agents_db_path, prepared.ref.uri) is not None:
        return True
    run_path = AgentHome.resolve(prepared.ref.home).room(prepared.ref.name).run_path
    if not run_path.exists():
        return False
    return ActivationState.load(run_path).status == "running"


def port_from_endpoint(endpoint: str) -> int | None:
    try:
        return int(endpoint.rsplit(":", 1)[1])
    except (IndexError, ValueError):
        return None
