from __future__ import annotations

from pathlib import Path

from .db import _connect, ensure_agent_registry
from .models import (
    KnownAgentRecord,
    KnownAgentSnapshot,
    RunningAgentRecord,
    RunningAgentSnapshot,
    _known_agent_from_row,
    _known_snapshot_from_row,
    _running_agent_from_row,
    _running_snapshot_from_row,
)


def find_known_agents_by_name(db_path: Path, name: str) -> list[KnownAgentRecord]:
    ensure_agent_registry(db_path)
    with _connect(db_path) as connection:
        rows = connection.execute(
            """
            SELECT agent_uri, agent_id, agent_name, agent_home, source_file, updated_at
            FROM agents
            WHERE agent_name = ?
            ORDER BY updated_at DESC
            """,
            (name,),
        ).fetchall()
    return [_known_agent_from_row(row) for row in rows]


def find_known_agents_by_id_prefix(db_path: Path, prefix: str) -> list[KnownAgentRecord]:
    ensure_agent_registry(db_path)
    with _connect(db_path) as connection:
        rows = connection.execute(
            """
            SELECT agent_uri, agent_id, agent_name, agent_home, source_file, updated_at
            FROM agents
            WHERE agent_id LIKE ?
            ORDER BY updated_at DESC
            """,
            (f"{prefix}%",),
        ).fetchall()
    return [_known_agent_from_row(row) for row in rows]


def get_running_agent(db_path: Path, agent_uri: str) -> RunningAgentRecord | None:
    ensure_agent_registry(db_path)
    with _connect(db_path) as connection:
        row = connection.execute(
            """
            SELECT agent_uri, pid, status, endpoint, sandbox, started_at, heartbeat_at
            FROM running_agents
            WHERE agent_uri = ?
            """,
            (agent_uri,),
        ).fetchone()
    if row is None:
        return None
    return _running_agent_from_row(row)


def list_running_agents(db_path: Path) -> list[RunningAgentSnapshot]:
    ensure_agent_registry(db_path)
    with _connect(db_path) as connection:
        rows = connection.execute(
            """
            SELECT
                agents.agent_uri,
                agents.agent_id,
                agents.agent_name,
                agents.agent_home,
                agents.source_file,
                running_agents.pid,
                running_agents.status,
                running_agents.endpoint,
                running_agents.sandbox,
                running_agents.started_at,
                running_agents.heartbeat_at
            FROM running_agents
            INNER JOIN agents ON agents.agent_uri = running_agents.agent_uri
            ORDER BY agents.agent_name ASC
            """
        ).fetchall()
    return [_running_snapshot_from_row(row) for row in rows]


def list_known_agents(db_path: Path) -> list[KnownAgentSnapshot]:
    ensure_agent_registry(db_path)
    with _connect(db_path) as connection:
        rows = connection.execute(
            """
            SELECT
                agents.agent_uri,
                agents.agent_id,
                agents.agent_name,
                agents.agent_home,
                agents.source_file,
                agents.updated_at,
                running_agents.pid,
                running_agents.status AS running_status,
                running_agents.endpoint,
                running_agents.sandbox,
                running_agents.started_at,
                running_agents.heartbeat_at
            FROM agents
            LEFT JOIN running_agents ON running_agents.agent_uri = agents.agent_uri
            ORDER BY agents.agent_name ASC, agents.updated_at DESC
            """
        ).fetchall()
    return [_known_snapshot_from_row(row) for row in rows]
