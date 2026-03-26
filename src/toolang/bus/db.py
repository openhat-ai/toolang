"""SQLite-backed bus event store and read models."""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from toolang.bus.events import (
    EVENT_TYPES,
    RUN_TYPES,
    AgentChanged,
    AgentCreated,
    AgentRemoved,
    AgentStarted,
    AgentStopped,
    BusEvent,
    RunFailed,
    RunFinished,
    RunStarted,
    serialize_event,
)


class StoredEvent(BaseModel):
    """Persisted bus event row with decoded payload."""

    event_id: int
    event_type: str
    at: str
    agent_uri: str
    agent_id: str
    run_id: str | None = None
    payload: dict[str, Any]


class AgentSnapshot(BaseModel):
    """Projected bus view of one known agent."""

    agent_uri: str
    agent_id: str
    name: str
    kind: str
    status: str
    endpoint: str | None = None
    sandbox: str | None = None
    agent_home: str | None = None
    source_file: str | None = None
    detail: str | None = None
    created_event_id: int | None = None
    created_at: str
    updated_at: str


class RunSnapshot(BaseModel):
    """Projected bus view of one recorded run."""

    run_id: str
    agent_uri: str
    agent_id: str
    run_type: str
    origin: str
    thunk_name: str | None = None
    summary: str | None = None
    status: str
    error: str | None = None
    parent_run_id: str | None = None
    thread_id: str | None = None
    created_at: str
    updated_at: str


class BusStore:
    """Append-only event store with agent and run projections."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path.as_posix(), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._init_schema()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def append(self, event: BusEvent) -> StoredEvent:
        record = serialize_event(event)
        event_type = str(record["event_type"])
        if event_type not in EVENT_TYPES:
            raise ValueError(f"unsupported event type: {event_type}")
        payload_text = json.dumps(record["payload"], ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            cursor = self._conn.execute(
                """
                INSERT INTO events(event_type, at, agent_uri, agent_id, run_id, payload_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    event_type,
                    record["at"],
                    record["agent_uri"],
                    record["agent_id"],
                    record["run_id"],
                    payload_text,
                ),
            )
            lastrowid = cursor.lastrowid
            if not isinstance(lastrowid, int):
                raise RuntimeError("sqlite did not return an integer lastrowid")
            event_id = lastrowid
            self._apply_projection(event, event_id)
            self._conn.commit()
        return StoredEvent(
            event_id=event_id,
            event_type=event_type,
            at=str(record["at"]),
            agent_uri=str(record["agent_uri"]),
            agent_id=str(record["agent_id"]),
            run_id=record["run_id"],
            payload=record["payload"],
        )

    def get_agent(self, agent_uri: str) -> AgentSnapshot | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT
                    agent_uri, agent_id, name, kind, status, endpoint, sandbox,
                    agent_home, source_file, detail, created_event_id, created_at, updated_at
                FROM agents
                WHERE agent_uri = ?
                """,
                (agent_uri,),
            ).fetchone()
        if row is None:
            return None
        return _agent_from_row(row)

    def get_agent_by_id(self, agent_id: str) -> AgentSnapshot | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT
                    agent_uri, agent_id, name, kind, status, endpoint, sandbox,
                    agent_home, source_file, detail, created_event_id, created_at, updated_at
                FROM agents
                WHERE agent_id = ?
                """,
                (agent_id,),
            ).fetchone()
        if row is None:
            return None
        return _agent_from_row(row)

    def list_agents(self, *, limit: int = 200) -> list[AgentSnapshot]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT
                    agent_uri, agent_id, name, kind, status, endpoint, sandbox,
                    agent_home, source_file, detail, created_event_id, created_at, updated_at
                FROM agents
                ORDER BY updated_at DESC, agent_id ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [_agent_from_row(row) for row in rows]

    def get_run(self, run_id: str) -> RunSnapshot | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT
                    run_id, agent_uri, agent_id, run_type, origin, thunk_name,
                    summary, status, error, parent_run_id, thread_id, created_at, updated_at
                FROM runs
                WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        return _run_from_row(row)

    def list_runs(self, *, agent_uri: str | None = None, limit: int = 50) -> list[RunSnapshot]:
        clauses: list[str] = []
        params: list[Any] = []
        if agent_uri is not None:
            clauses.append("agent_uri = ?")
            params.append(agent_uri)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT
                    run_id, agent_uri, agent_id, run_type, origin, thunk_name,
                    summary, status, error, parent_run_id, thread_id, created_at, updated_at
                FROM runs
                {where}
                ORDER BY updated_at DESC, run_id DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [_run_from_row(row) for row in rows]

    def list_events(
        self,
        *,
        agent_uri: str | None = None,
        from_event_id: int = 0,
        to_event_id: int | None = None,
        run_id: str | None = None,
        limit: int = 100,
    ) -> list[StoredEvent]:
        clauses = ["event_id > ?"]
        params: list[Any] = [from_event_id]
        if to_event_id is not None:
            clauses.append("event_id <= ?")
            params.append(to_event_id)
        if agent_uri is not None:
            clauses.append("agent_uri = ?")
            params.append(agent_uri)
        if run_id is not None:
            clauses.append("run_id = ?")
            params.append(run_id)
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT event_id, event_type, at, agent_uri, agent_id, run_id, payload_json
                FROM events
                WHERE {' AND '.join(clauses)}
                ORDER BY event_id ASC
                LIMIT ?
                """,
                params,
            ).fetchall()
        items: list[StoredEvent] = []
        for row in rows:
            items.append(
                StoredEvent(
                    event_id=int(row["event_id"]),
                    event_type=str(row["event_type"]),
                    at=str(row["at"]),
                    agent_uri=str(row["agent_uri"]),
                    agent_id=str(row["agent_id"]),
                    run_id=row["run_id"],
                    payload=json.loads(str(row["payload_json"])),
                )
            )
        return items

    def max_event_id(self, *, agent_uri: str | None = None) -> int:
        clauses: list[str] = []
        params: list[Any] = []
        if agent_uri is not None:
            clauses.append("agent_uri = ?")
            params.append(agent_uri)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock:
            row = self._conn.execute(
                f"SELECT COALESCE(MAX(event_id), 0) AS event_id FROM events {where}",
                params,
            ).fetchone()
        if row is None:
            return 0
        return int(row["event_id"])

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL;")
            self._conn.execute("PRAGMA synchronous=NORMAL;")
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    at TEXT NOT NULL,
                    agent_uri TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    run_id TEXT,
                    payload_json TEXT NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS agents (
                    agent_uri TEXT PRIMARY KEY,
                    agent_id TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    endpoint TEXT,
                    sandbox TEXT,
                    agent_home TEXT,
                    source_file TEXT,
                    detail TEXT,
                    created_event_id INTEGER,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    agent_uri TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    run_type TEXT NOT NULL,
                    origin TEXT NOT NULL,
                    thunk_name TEXT,
                    summary TEXT,
                    status TEXT NOT NULL,
                    error TEXT,
                    parent_run_id TEXT,
                    thread_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_events_agent ON events(agent_uri, event_id)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_runs_agent ON runs(agent_uri, updated_at)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_agents_status ON agents(status)"
            )
            columns = {
                str(row["name"]): row
                for row in self._conn.execute("PRAGMA table_info(agents)").fetchall()
            }
            if "sandbox" not in columns:
                self._conn.execute("ALTER TABLE agents ADD COLUMN sandbox TEXT")
            if "created_event_id" not in columns:
                self._conn.execute("ALTER TABLE agents ADD COLUMN created_event_id INTEGER")
            self._conn.commit()

    def _apply_projection(self, event: BusEvent, event_id: int) -> None:
        if isinstance(event, AgentCreated):
            self._apply_agent_created(event, event_id)
            return
        if isinstance(event, AgentRemoved):
            self._apply_agent_removed(event)
            return
        if isinstance(event, AgentStarted):
            self._apply_agent_started(event)
            return
        if isinstance(event, AgentStopped):
            self._apply_agent_stopped(event)
            return
        if isinstance(event, AgentChanged):
            self._apply_agent_changed(event)
            return
        self._apply_run_event(event)

    def _apply_agent_created(self, event: AgentCreated, event_id: int) -> None:
        self._conn.execute(
            """
            INSERT INTO agents(
                agent_uri, agent_id, name, kind, status, endpoint, sandbox,
                agent_home, source_file, detail, created_event_id, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(agent_uri) DO UPDATE SET
                agent_id = excluded.agent_id,
                name = excluded.name,
                kind = excluded.kind,
                status = excluded.status,
                endpoint = excluded.endpoint,
                sandbox = excluded.sandbox,
                agent_home = excluded.agent_home,
                source_file = excluded.source_file,
                detail = excluded.detail,
                created_event_id = excluded.created_event_id,
                created_at = excluded.created_at,
                updated_at = excluded.updated_at
            """,
            (
                event.agent_uri,
                event.agent_id,
                event.name,
                event.kind,
                "dormant",
                None,
                None,
                event.agent_home,
                event.source_file,
                event.detail,
                event_id,
                event.at,
                event.at,
            ),
        )

    def _apply_agent_removed(self, event: AgentRemoved) -> None:
        row = self._conn.execute(
            """
            SELECT kind, created_event_id, created_at
            FROM agents
            WHERE agent_uri = ?
            """,
            (event.agent_uri,),
        ).fetchone()
        kind = str(row["kind"]) if row is not None and row["kind"] else event.kind
        created_event_id = (
            int(row["created_event_id"])
            if row is not None and row["created_event_id"] is not None
            else None
        )
        created_at = str(row["created_at"]) if row is not None else event.at
        self._conn.execute(
            """
            INSERT INTO agents(
                agent_uri, agent_id, name, kind, status, endpoint, sandbox,
                agent_home, source_file, detail, created_event_id, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(agent_uri) DO UPDATE SET
                agent_id = excluded.agent_id,
                name = excluded.name,
                kind = excluded.kind,
                status = excluded.status,
                endpoint = excluded.endpoint,
                sandbox = excluded.sandbox,
                agent_home = COALESCE(excluded.agent_home, agents.agent_home),
                source_file = COALESCE(excluded.source_file, agents.source_file),
                detail = excluded.detail,
                created_event_id = COALESCE(excluded.created_event_id, agents.created_event_id),
                created_at = excluded.created_at,
                updated_at = excluded.updated_at
            """,
            (
                event.agent_uri,
                event.agent_id,
                event.name,
                kind,
                "removed",
                None,
                None,
                event.agent_home,
                event.source_file,
                event.detail,
                created_event_id,
                created_at,
                event.at,
            ),
        )

    def _apply_agent_started(self, event: AgentStarted) -> None:
        row = self._conn.execute(
            "SELECT created_event_id, created_at FROM agents WHERE agent_uri = ?",
            (event.agent_uri,),
        ).fetchone()
        created_event_id = (
            int(row["created_event_id"])
            if row is not None and row["created_event_id"] is not None
            else None
        )
        created_at = str(row["created_at"]) if row is not None else event.at
        self._conn.execute(
            """
            INSERT INTO agents(
                agent_uri, agent_id, name, kind, status, endpoint, sandbox,
                agent_home, source_file, detail, created_event_id, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(agent_uri) DO UPDATE SET
                agent_id = excluded.agent_id,
                name = excluded.name,
                kind = excluded.kind,
                status = excluded.status,
                endpoint = excluded.endpoint,
                sandbox = excluded.sandbox,
                agent_home = excluded.agent_home,
                source_file = excluded.source_file,
                detail = excluded.detail,
                created_event_id = COALESCE(excluded.created_event_id, agents.created_event_id),
                updated_at = excluded.updated_at
            """,
            (
                event.agent_uri,
                event.agent_id,
                event.name,
                event.kind,
                "started",
                event.endpoint,
                event.sandbox,
                event.agent_home,
                event.source_file,
                None,
                created_event_id,
                created_at,
                event.at,
            ),
        )

    def _apply_agent_stopped(self, event: AgentStopped) -> None:
        row = self._conn.execute(
            "SELECT created_event_id, created_at FROM agents WHERE agent_uri = ?",
            (event.agent_uri,),
        ).fetchone()
        created_event_id = (
            int(row["created_event_id"])
            if row is not None and row["created_event_id"] is not None
            else None
        )
        created_at = str(row["created_at"]) if row is not None else event.at
        self._conn.execute(
            """
            INSERT INTO agents(
                agent_uri, agent_id, name, kind, status, endpoint, sandbox,
                agent_home, source_file, detail, created_event_id, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(agent_uri) DO UPDATE SET
                agent_id = excluded.agent_id,
                name = excluded.name,
                status = excluded.status,
                endpoint = COALESCE(excluded.endpoint, agents.endpoint),
                sandbox = COALESCE(excluded.sandbox, agents.sandbox),
                agent_home = COALESCE(excluded.agent_home, agents.agent_home),
                source_file = COALESCE(excluded.source_file, agents.source_file),
                detail = excluded.detail,
                created_event_id = COALESCE(excluded.created_event_id, agents.created_event_id),
                updated_at = excluded.updated_at
            """,
            (
                event.agent_uri,
                event.agent_id,
                event.name,
                "",
                "stopped",
                event.endpoint,
                event.sandbox,
                event.agent_home,
                event.source_file,
                event.detail,
                created_event_id,
                created_at,
                event.at,
            ),
        )

    def _apply_agent_changed(self, event: AgentChanged) -> None:
        row = self._conn.execute(
            """
            SELECT kind, status, endpoint, sandbox, created_event_id, created_at
            FROM agents
            WHERE agent_uri = ?
            """,
            (event.agent_uri,),
        ).fetchone()
        kind = str(row["kind"]) if row is not None else ""
        status = str(row["status"]) if row is not None else "updated"
        endpoint = row["endpoint"] if row is not None else None
        sandbox = row["sandbox"] if row is not None else None
        created_event_id = (
            int(row["created_event_id"])
            if row is not None and row["created_event_id"] is not None
            else None
        )
        created_at = str(row["created_at"]) if row is not None else event.at
        self._conn.execute(
            """
            INSERT INTO agents(
                agent_uri, agent_id, name, kind, status, endpoint, sandbox,
                agent_home, source_file, detail, created_event_id, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(agent_uri) DO UPDATE SET
                agent_id = excluded.agent_id,
                name = excluded.name,
                endpoint = COALESCE(excluded.endpoint, agents.endpoint),
                sandbox = COALESCE(excluded.sandbox, agents.sandbox),
                agent_home = COALESCE(excluded.agent_home, agents.agent_home),
                source_file = COALESCE(excluded.source_file, agents.source_file),
                detail = excluded.detail,
                created_event_id = COALESCE(excluded.created_event_id, agents.created_event_id),
                updated_at = excluded.updated_at
            """,
            (
                event.agent_uri,
                event.agent_id,
                event.name,
                kind,
                status,
                endpoint,
                sandbox,
                event.agent_home,
                event.source_file,
                event.detail,
                created_event_id,
                created_at,
                event.at,
            ),
        )

    def _apply_run_event(self, event: RunStarted | RunFinished | RunFailed) -> None:
        if event.run_type not in RUN_TYPES:
            raise ValueError(f"unsupported run type: {event.run_type}")
        row = self._conn.execute(
            "SELECT created_at FROM runs WHERE run_id = ?",
            (event.run_id,),
        ).fetchone()
        created_at = str(row["created_at"]) if row is not None else event.at
        summary = event.summary if isinstance(event, RunStarted | RunFinished) else None
        error = event.error if isinstance(event, RunFailed) else None
        status = event.event_type.removeprefix("run_")
        self._conn.execute(
            """
            INSERT INTO runs(
                run_id, agent_uri, agent_id, run_type, origin, thunk_name,
                summary, status, error, parent_run_id, thread_id, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id) DO UPDATE SET
                agent_uri = excluded.agent_uri,
                agent_id = excluded.agent_id,
                run_type = excluded.run_type,
                origin = excluded.origin,
                thunk_name = excluded.thunk_name,
                summary = COALESCE(excluded.summary, runs.summary),
                status = excluded.status,
                error = COALESCE(excluded.error, runs.error),
                parent_run_id = COALESCE(excluded.parent_run_id, runs.parent_run_id),
                thread_id = COALESCE(excluded.thread_id, runs.thread_id),
                updated_at = excluded.updated_at
            """,
            (
                event.run_id,
                event.agent_uri,
                event.agent_id,
                event.run_type,
                event.origin,
                event.thunk_name,
                summary,
                status,
                error,
                event.parent_run_id,
                event.thread_id,
                created_at,
                event.at,
            ),
        )


def _agent_from_row(row: sqlite3.Row) -> AgentSnapshot:
    return AgentSnapshot(
        agent_uri=str(row["agent_uri"]),
        agent_id=str(row["agent_id"]),
        name=str(row["name"]),
        kind=str(row["kind"]),
        status=str(row["status"]),
        endpoint=row["endpoint"],
        sandbox=row["sandbox"],
        agent_home=row["agent_home"],
        source_file=row["source_file"],
        detail=row["detail"],
        created_event_id=(
            int(row["created_event_id"])
            if row["created_event_id"] is not None
            else None
        ),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _run_from_row(row: sqlite3.Row) -> RunSnapshot:
    return RunSnapshot(
        run_id=str(row["run_id"]),
        agent_uri=str(row["agent_uri"]),
        agent_id=str(row["agent_id"]),
        run_type=str(row["run_type"]),
        origin=str(row["origin"]),
        thunk_name=row["thunk_name"],
        summary=row["summary"],
        status=str(row["status"]),
        error=row["error"],
        parent_run_id=row["parent_run_id"],
        thread_id=row["thread_id"],
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )
