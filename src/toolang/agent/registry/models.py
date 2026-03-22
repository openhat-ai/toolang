"""Typed records used by the local known-agent registry."""

from __future__ import annotations

import sqlite3
from datetime import datetime

from pydantic import BaseModel

from toolang_concepts.identity import AgentRef


class KnownAgentRecord(BaseModel):
    """Stored identity row for one known agent."""

    agent_uri: str
    agent_id: str
    agent_name: str
    agent_home: str
    source_file: str
    updated_at: datetime

    @classmethod
    def from_resolved_agent(
        cls,
        agent: AgentRef,
        *,
        updated_at: datetime,
    ) -> "KnownAgentRecord":
        return cls(
            agent_uri=agent.uri,
            agent_id=agent.id[:12],
            agent_name=agent.name,
            agent_home=str(agent.home),
            source_file=agent.source.name,
            updated_at=updated_at,
        )


class RunningAgentRecord(BaseModel):
    """Stored running-state row for one active agent."""

    agent_uri: str
    pid: int | None = None
    status: str
    endpoint: str | None = None
    sandbox: str = "host"
    started_at: datetime
    heartbeat_at: datetime


class RunningAgentSnapshot(BaseModel):
    """Joined view of one active agent with stable identity fields."""

    agent_uri: str
    agent_id: str
    agent_name: str
    agent_home: str
    source_file: str
    pid: int | None = None
    status: str
    endpoint: str | None = None
    sandbox: str = "host"
    started_at: datetime
    heartbeat_at: datetime


class KnownAgentSnapshot(BaseModel):
    """Known agent record with optional running-state fields attached."""

    agent_uri: str
    agent_id: str
    agent_name: str
    agent_home: str
    source_file: str
    updated_at: datetime
    pid: int | None = None
    running_status: str | None = None
    endpoint: str | None = None
    sandbox: str | None = None
    started_at: datetime | None = None
    heartbeat_at: datetime | None = None


def _known_agent_from_row(row: sqlite3.Row) -> KnownAgentRecord:
    return KnownAgentRecord(
        agent_uri=row["agent_uri"],
        agent_id=row["agent_id"],
        agent_name=row["agent_name"],
        agent_home=row["agent_home"],
        source_file=row["source_file"],
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


def _running_agent_from_row(row: sqlite3.Row) -> RunningAgentRecord:
    return RunningAgentRecord(
        agent_uri=row["agent_uri"],
        pid=row["pid"],
        status=row["status"],
        endpoint=row["endpoint"],
        sandbox=row["sandbox"],
        started_at=datetime.fromisoformat(row["started_at"]),
        heartbeat_at=datetime.fromisoformat(row["heartbeat_at"]),
    )


def _running_snapshot_from_row(row: sqlite3.Row) -> RunningAgentSnapshot:
    return RunningAgentSnapshot(
        agent_uri=row["agent_uri"],
        agent_id=row["agent_id"],
        agent_name=row["agent_name"],
        agent_home=row["agent_home"],
        source_file=row["source_file"],
        pid=row["pid"],
        status=row["status"],
        endpoint=row["endpoint"],
        sandbox=row["sandbox"],
        started_at=datetime.fromisoformat(row["started_at"]),
        heartbeat_at=datetime.fromisoformat(row["heartbeat_at"]),
    )


def _known_snapshot_from_row(row: sqlite3.Row) -> KnownAgentSnapshot:
    return KnownAgentSnapshot(
        agent_uri=row["agent_uri"],
        agent_id=row["agent_id"],
        agent_name=row["agent_name"],
        agent_home=row["agent_home"],
        source_file=row["source_file"],
        updated_at=datetime.fromisoformat(row["updated_at"]),
        pid=row["pid"],
        running_status=row["running_status"],
        endpoint=row["endpoint"],
        sandbox=row["sandbox"],
        started_at=(
            datetime.fromisoformat(row["started_at"])
            if row["started_at"] is not None
            else None
        ),
        heartbeat_at=(
            datetime.fromisoformat(row["heartbeat_at"])
            if row["heartbeat_at"] is not None
            else None
        ),
    )
