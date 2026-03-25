from __future__ import annotations

from contextlib import contextmanager
import sqlite3
from pathlib import Path
from typing import Iterator


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
                pid INTEGER,
                status TEXT NOT NULL,
                endpoint TEXT,
                sandbox TEXT NOT NULL DEFAULT 'host',
                started_at TEXT NOT NULL,
                heartbeat_at TEXT NOT NULL,
                FOREIGN KEY(agent_uri) REFERENCES agents(agent_uri) ON DELETE CASCADE
            )
            """
        )
        _ensure_running_agents_schema(connection)
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_running_agents_status
            ON running_agents(status)
            """
        )
        connection.commit()


@contextmanager
def _connect(db_path: Path) -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        yield connection
    finally:
        connection.close()


def _ensure_running_agents_schema(connection: sqlite3.Connection) -> None:
    columns = {
        str(row["name"]): row
        for row in connection.execute("PRAGMA table_info(running_agents)").fetchall()
    }
    pid_info = columns.get("pid")
    needs_rebuild = pid_info is not None and bool(pid_info["notnull"])
    if needs_rebuild:
        connection.execute(
            """
            CREATE TABLE running_agents_new (
                agent_uri TEXT PRIMARY KEY,
                pid INTEGER,
                status TEXT NOT NULL,
                endpoint TEXT,
                sandbox TEXT NOT NULL DEFAULT 'host',
                started_at TEXT NOT NULL,
                heartbeat_at TEXT NOT NULL,
                FOREIGN KEY(agent_uri) REFERENCES agents(agent_uri) ON DELETE CASCADE
            )
            """
        )
        if "sandbox" in columns:
            connection.execute(
                """
                INSERT INTO running_agents_new(
                    agent_uri, pid, status, endpoint, sandbox, started_at, heartbeat_at
                )
                SELECT
                    agent_uri,
                    pid,
                    status,
                    endpoint,
                    COALESCE(sandbox, 'host'),
                    started_at,
                    heartbeat_at
                FROM running_agents
                """
            )
        else:
            connection.execute(
                """
                INSERT INTO running_agents_new(
                    agent_uri, pid, status, endpoint, sandbox, started_at, heartbeat_at
                )
                SELECT
                    agent_uri,
                    pid,
                    status,
                    endpoint,
                    'host',
                    started_at,
                    heartbeat_at
                FROM running_agents
                """
            )
        connection.execute("DROP TABLE running_agents")
        connection.execute("ALTER TABLE running_agents_new RENAME TO running_agents")
        return
    if "sandbox" not in columns:
        connection.execute(
            "ALTER TABLE running_agents ADD COLUMN sandbox TEXT NOT NULL DEFAULT 'host'"
        )
