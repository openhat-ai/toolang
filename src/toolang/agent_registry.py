from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel

from toolang.agent_refs import ResolvedAgentRef


class KnownAgentRecord(BaseModel):
    agent_uri: str
    agent_id: str
    agent_name: str
    agent_home: str
    source_file: str
    updated_at: datetime

    @classmethod
    def from_resolved_agent(
        cls,
        agent: ResolvedAgentRef,
        *,
        updated_at: datetime,
    ) -> "KnownAgentRecord":
        return cls(
            agent_uri=agent.agent_uri,
            agent_id=agent.agent_id[:12],
            agent_name=agent.agent_name,
            agent_home=str(agent.agent_home),
            source_file=agent.source_path.name,
            updated_at=updated_at,
        )


class RunningAgentRecord(BaseModel):
    agent_uri: str
    pid: int
    status: str
    endpoint: str | None = None
    started_at: datetime
    heartbeat_at: datetime


class RunningAgentSnapshot(BaseModel):
    agent_uri: str
    agent_id: str
    agent_name: str
    agent_home: str
    source_file: str
    pid: int
    status: str
    endpoint: str | None = None
    started_at: datetime
    heartbeat_at: datetime


class KnownAgentSnapshot(BaseModel):
    agent_uri: str
    agent_id: str
    agent_name: str
    agent_home: str
    source_file: str
    updated_at: datetime
    pid: int | None = None
    running_status: str | None = None
    endpoint: str | None = None
    started_at: datetime | None = None
    heartbeat_at: datetime | None = None


def ensure_agent_registry(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with _connect(db_path) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS agents (
                agent_uri TEXT PRIMARY KEY,
                agent_id TEXT NOT NULL UNIQUE,
                agent_name TEXT NOT NULL,
                agent_home TEXT NOT NULL,
                source_file TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_agents_agent_name
            ON agents(agent_name)
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS running_agents (
                agent_uri TEXT PRIMARY KEY,
                pid INTEGER NOT NULL,
                status TEXT NOT NULL,
                endpoint TEXT,
                started_at TEXT NOT NULL,
                heartbeat_at TEXT NOT NULL,
                FOREIGN KEY(agent_uri) REFERENCES agents(agent_uri) ON DELETE CASCADE
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_running_agents_status
            ON running_agents(status)
            """
        )
        connection.commit()


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


def upsert_running_agent(db_path: Path, record: RunningAgentRecord) -> None:
    ensure_agent_registry(db_path)
    with _connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO running_agents (
                agent_uri, pid, status, endpoint, started_at, heartbeat_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(agent_uri) DO UPDATE SET
                pid = excluded.pid,
                status = excluded.status,
                endpoint = excluded.endpoint,
                started_at = excluded.started_at,
                heartbeat_at = excluded.heartbeat_at
            """,
            (
                record.agent_uri,
                record.pid,
                record.status,
                record.endpoint,
                record.started_at.isoformat(),
                record.heartbeat_at.isoformat(),
            ),
        )
        connection.commit()


def get_running_agent(db_path: Path, agent_uri: str) -> RunningAgentRecord | None:
    ensure_agent_registry(db_path)
    with _connect(db_path) as connection:
        row = connection.execute(
            """
            SELECT agent_uri, pid, status, endpoint, started_at, heartbeat_at
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
                running_agents.started_at,
                running_agents.heartbeat_at
            FROM agents
            LEFT JOIN running_agents ON running_agents.agent_uri = agents.agent_uri
            ORDER BY agents.agent_name ASC, agents.updated_at DESC
            """
        ).fetchall()
    return [_known_snapshot_from_row(row) for row in rows]


def delete_running_agent(db_path: Path, agent_uri: str) -> None:
    ensure_agent_registry(db_path)
    with _connect(db_path) as connection:
        connection.execute(
            "DELETE FROM running_agents WHERE agent_uri = ?",
            (agent_uri,),
        )
        connection.commit()


def _connect(db_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


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
