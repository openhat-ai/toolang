from __future__ import annotations

from pathlib import Path

from .db import _connect, ensure_agent_registry
from .models import KnownAgentRecord, RunningAgentRecord


def upsert_known_agent(db_path: Path, record: KnownAgentRecord) -> None:
    ensure_agent_registry(db_path)
    with _connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO agents (
                agent_uri, agent_id, agent_name, agent_home, source_file, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(agent_uri) DO UPDATE SET
                agent_id = excluded.agent_id,
                agent_name = excluded.agent_name,
                agent_home = excluded.agent_home,
                source_file = excluded.source_file,
                updated_at = excluded.updated_at
            """,
            (
                record.agent_uri,
                record.agent_id,
                record.agent_name,
                record.agent_home,
                record.source_file,
                record.updated_at.isoformat(),
            ),
        )
        connection.commit()


def upsert_running_agent(db_path: Path, record: RunningAgentRecord) -> None:
    ensure_agent_registry(db_path)
    with _connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO running_agents (
                agent_uri, pid, status, endpoint, sandbox, started_at, heartbeat_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(agent_uri) DO UPDATE SET
                pid = excluded.pid,
                status = excluded.status,
                endpoint = excluded.endpoint,
                sandbox = excluded.sandbox,
                started_at = excluded.started_at,
                heartbeat_at = excluded.heartbeat_at
            """,
            (
                record.agent_uri,
                record.pid,
                record.status,
                record.endpoint,
                record.sandbox,
                record.started_at.isoformat(),
                record.heartbeat_at.isoformat(),
            ),
        )
        connection.commit()


def delete_running_agent(db_path: Path, agent_uri: str) -> None:
    ensure_agent_registry(db_path)
    with _connect(db_path) as connection:
        connection.execute(
            "DELETE FROM running_agents WHERE agent_uri = ?",
            (agent_uri,),
        )
        connection.commit()


def delete_known_agent(db_path: Path, agent_uri: str) -> bool:
    ensure_agent_registry(db_path)
    with _connect(db_path) as connection:
        cursor = connection.execute(
            "DELETE FROM agents WHERE agent_uri = ?",
            (agent_uri,),
        )
        connection.commit()
    return cursor.rowcount > 0
