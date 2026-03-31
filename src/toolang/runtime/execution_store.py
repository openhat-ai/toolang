"""Execution truth-layer storage for activations, threads, runs, steps, and messages."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from toolang.concepts.execution import (
    ActivationKind,
    ActivationRecord,
    ActivationStatus,
    ExecutionStrategy,
    MessageOrigin,
    MessageSender,
    RunMessageRecord,
    RunRecord,
    RunStatus,
    RuntimeLoop,
    StepKind,
    StepRecord,
    StepStatus,
    ThreadGroup,
    ThreadRecord,
)
from toolang.concepts.identity import AgentRef
from toolang.concepts.messages import MessageRole, TextPart, TurnMessage, part_from_dict, part_to_dict


def utc_now() -> str:
    """Return the current UTC time in Z-normalized ISO format."""

    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class ExecutionStore:
    """SQLite-backed truth layer for activations, threads, runs, steps, and transcript messages."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path.as_posix(), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._init_schema()

    def close(self) -> None:
        """Close the underlying SQLite connection."""

        with self._lock:
            self._conn.close()

    def begin_activation(
        self,
        *,
        agent: AgentRef,
        activation_id: str,
        activation_kind: ActivationKind,
        sandbox: str,
        cap_scopes: tuple[str, ...],
        runtime_loops: tuple[RuntimeLoop, ...] = (),
        started_at: str | None = None,
    ) -> ActivationRecord:
        """Persist one newly started activation."""

        now = started_at or utc_now()
        runtime_loops_json = _dump_json(list(runtime_loops))
        cap_scopes_json = _dump_json(list(cap_scopes))
        plugin_snapshot_json = _dump_json({})
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO activations(
                    activation_id,
                    agent_uri,
                    agent_id,
                    agent_name,
                    activation_kind,
                    status,
                    started_at,
                    finished_at,
                    runtime_loops_json,
                    sandbox,
                    cap_scopes_json,
                    sync_fingerprint,
                    plugin_snapshot_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, NULL, ?)
                """,
                (
                    activation_id,
                    agent.uri,
                    agent.id,
                    agent.name,
                    activation_kind,
                    "running",
                    now,
                    runtime_loops_json,
                    sandbox,
                    cap_scopes_json,
                    plugin_snapshot_json,
                ),
            )
            row = self._conn.execute(
                "SELECT * FROM activations WHERE activation_id = ?",
                (activation_id,),
            ).fetchone()
            self._conn.commit()
        if row is None:
            raise RuntimeError("activation insert returned no row")
        return _activation_from_row(row)

    def finish_activation(
        self,
        *,
        activation_id: str,
        status: ActivationStatus,
        finished_at: str | None = None,
    ) -> ActivationRecord:
        """Persist one completed or stopped activation."""

        now = finished_at or utc_now()
        with self._lock:
            self._conn.execute(
                """
                UPDATE activations
                SET status = ?, finished_at = ?
                WHERE activation_id = ?
                """,
                (status, now, activation_id),
            )
            row = self._conn.execute(
                "SELECT * FROM activations WHERE activation_id = ?",
                (activation_id,),
            ).fetchone()
            self._conn.commit()
        if row is None:
            raise RuntimeError(f"activation not found: {activation_id}")
        return _activation_from_row(row)

    def get_activation(self, *, activation_id: str) -> ActivationRecord | None:
        """Return one activation by id."""

        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM activations WHERE activation_id = ?",
                (activation_id,),
            ).fetchone()
        if row is None:
            return None
        return _activation_from_row(row)

    def list_activations(self, *, agent_uri: str | None = None) -> list[ActivationRecord]:
        """List persisted activations, newest first."""

        if agent_uri is None:
            query = "SELECT * FROM activations ORDER BY started_at DESC"
            params: tuple[Any, ...] = ()
        else:
            query = (
                "SELECT * FROM activations WHERE agent_uri = ? ORDER BY started_at DESC"
            )
            params = (agent_uri,)
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [_activation_from_row(row) for row in rows]

    def ensure_thread(
        self,
        *,
        agent: AgentRef,
        thread_id: str,
        thread_group: ThreadGroup,
        title: str | None = None,
        at: str | None = None,
    ) -> ThreadRecord:
        """Create or refresh one execution thread."""

        now = at or utc_now()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO threads(
                    thread_id, agent_uri, thread_group, title, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(thread_id) DO UPDATE SET
                    agent_uri = excluded.agent_uri,
                    thread_group = excluded.thread_group,
                    title = COALESCE(threads.title, excluded.title),
                    updated_at = excluded.updated_at
                """,
                (thread_id, agent.uri, thread_group, title, now, now),
            )
            row = self._conn.execute(
                "SELECT * FROM threads WHERE thread_id = ?",
                (thread_id,),
            ).fetchone()
            self._conn.commit()
        if row is None:
            raise RuntimeError("thread upsert returned no row")
        return _thread_from_row(row)

    def get_thread(self, *, thread_id: str) -> ThreadRecord | None:
        """Return one thread by id."""

        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM threads WHERE thread_id = ?",
                (thread_id,),
            ).fetchone()
        if row is None:
            return None
        return _thread_from_row(row)

    def list_threads(
        self,
        *,
        agent_uri: str | None = None,
        limit: int = 50,
    ) -> list[ThreadRecord]:
        """List persisted threads, newest first."""

        if agent_uri is None:
            query = "SELECT * FROM threads ORDER BY updated_at DESC LIMIT ?"
            params: tuple[Any, ...] = (limit,)
        else:
            query = (
                "SELECT * FROM threads WHERE agent_uri = ? ORDER BY updated_at DESC LIMIT ?"
            )
            params = (agent_uri, limit)
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [_thread_from_row(row) for row in rows]

    def append_message(
        self,
        *,
        thread_id: str,
        run_id: str,
        role: MessageRole,
        origin: MessageOrigin,
        channel: str | None,
        sender: MessageSender,
        text: str,
        message: TurnMessage | None = None,
        meta: dict[str, Any] | None = None,
        at: str | None = None,
    ) -> RunMessageRecord:
        """Append one transcript message to an existing thread."""

        if role not in {"user", "assistant"}:
            raise ValueError(f"unsupported role: {role}")
        now = at or utc_now()
        effective_message = message or TurnMessage(
            id=f"{run_id}:{role}",
            role=role,
            parts=(TextPart(id=f"{run_id}:{role}:text:1", text=text),),
            created_at=now,
            metadata=dict(meta or {}),
        )
        preview = effective_message.preview_text() or text
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(seq), 0) AS seq FROM messages WHERE thread_id = ?",
                (thread_id,),
            ).fetchone()
            next_seq = int(row["seq"]) + 1 if row is not None else 1
            cursor = self._conn.execute(
                """
                INSERT INTO messages(
                    message_id,
                    thread_id,
                    run_id,
                    seq,
                    role,
                    origin,
                    channel,
                    sender,
                    text,
                    created_at,
                    meta_json,
                    parts_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    effective_message.id,
                    thread_id,
                    run_id,
                    next_seq,
                    role,
                    origin,
                    channel,
                    sender,
                    preview,
                    now,
                    _dump_json(meta or {}),
                    _dump_json([part_to_dict(item) for item in effective_message.parts]),
                ),
            )
            self._conn.execute(
                "UPDATE threads SET updated_at = ? WHERE thread_id = ?",
                (now, thread_id),
            )
            inserted = self._conn.execute(
                """
                SELECT
                    message_id,
                    thread_id,
                    run_id,
                    seq,
                    role,
                    origin,
                    channel,
                    sender,
                    text,
                    created_at,
                    meta_json,
                    parts_json
                FROM messages
                WHERE rowid = ?
                """,
                (cursor.lastrowid,),
            ).fetchone()
            self._conn.commit()
        if inserted is None:
            raise RuntimeError("message insert returned no row")
        return _message_from_row(inserted)

    def recent_messages(
        self,
        *,
        thread_id: str,
        limit: int = 20,
    ) -> list[RunMessageRecord]:
        """Return recent transcript messages for one thread."""

        with self._lock:
            rows = self._conn.execute(
                """
                SELECT
                    message_id,
                    thread_id,
                    run_id,
                    seq,
                    role,
                    origin,
                    channel,
                    sender,
                    text,
                    created_at,
                    meta_json,
                    parts_json
                FROM messages
                WHERE thread_id = ?
                ORDER BY seq DESC
                LIMIT ?
                """,
                (thread_id, limit),
            ).fetchall()
        return [_message_from_row(row) for row in reversed(rows)]

    def recent_openai_messages(
        self,
        *,
        thread_id: str,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Return provider-ready chat history for one thread."""

        return [
            {
                "role": message.role,
                "content": message.text,
            }
            for message in self.recent_messages(thread_id=thread_id, limit=limit)
        ]

    def messages_for_run(self, *, run_id: str) -> list[RunMessageRecord]:
        """Return transcript messages for one run."""

        with self._lock:
            rows = self._conn.execute(
                """
                SELECT
                    message_id,
                    thread_id,
                    run_id,
                    seq,
                    role,
                    origin,
                    channel,
                    sender,
                    text,
                    created_at,
                    meta_json,
                    parts_json
                FROM messages
                WHERE run_id = ?
                ORDER BY seq ASC
                """,
                (run_id,),
            ).fetchall()
        return [_message_from_row(row) for row in rows]

    def thread_preview(self, *, thread_id: str) -> tuple[str | None, str | None]:
        """Return the latest preview text and channel for one thread."""

        with self._lock:
            row = self._conn.execute(
                """
                SELECT text, channel
                FROM messages
                WHERE thread_id = ?
                ORDER BY seq DESC
                LIMIT 1
                """,
                (thread_id,),
            ).fetchone()
        if row is None:
            return None, None
        return (
            str(row["text"]) if row["text"] is not None else None,
            str(row["channel"]) if row["channel"] is not None else None,
        )

    def start_run(
        self,
        *,
        run_id: str,
        activation_id: str,
        thread_id: str,
        origin: MessageOrigin,
        channel: str | None,
        sender: MessageSender,
        execution_strategy: ExecutionStrategy,
        input_text: str | None,
        at: str | None = None,
    ) -> RunRecord:
        """Persist one newly started run."""

        now = at or utc_now()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO runs(
                    run_id,
                    activation_id,
                    thread_id,
                    origin,
                    channel,
                    sender,
                    execution_strategy,
                    status,
                    input_text,
                    output_text,
                    error,
                    created_at,
                    started_at,
                    finished_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, NULL)
                """,
                (
                    run_id,
                    activation_id,
                    thread_id,
                    origin,
                    channel,
                    sender,
                    execution_strategy,
                    "running",
                    input_text,
                    now,
                    now,
                ),
            )
            self._conn.execute(
                "UPDATE threads SET updated_at = ? WHERE thread_id = ?",
                (now, thread_id),
            )
            row = self._conn.execute(
                "SELECT * FROM runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            self._conn.commit()
        if row is None:
            raise RuntimeError("run insert returned no row")
        return _run_from_row(row)

    def get_run(self, *, run_id: str) -> RunRecord | None:
        """Return one run by id."""

        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        return _run_from_row(row)

    def finish_run(
        self,
        *,
        run_id: str,
        output_text: str,
        status: RunStatus = "finished",
        finished_at: str | None = None,
    ) -> RunRecord:
        """Persist one completed run."""

        now = finished_at or utc_now()
        with self._lock:
            self._conn.execute(
                """
                UPDATE runs
                SET status = ?, output_text = ?, finished_at = ?
                WHERE run_id = ?
                """,
                (status, output_text, now, run_id),
            )
            row = self._conn.execute(
                "SELECT * FROM runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            self._conn.commit()
        if row is None:
            raise RuntimeError(f"run not found: {run_id}")
        return _run_from_row(row)

    def fail_run(
        self,
        *,
        run_id: str,
        error: str,
        finished_at: str | None = None,
    ) -> RunRecord:
        """Persist one failed run."""

        now = finished_at or utc_now()
        with self._lock:
            self._conn.execute(
                """
                UPDATE runs
                SET status = ?, error = ?, finished_at = ?
                WHERE run_id = ?
                """,
                ("failed", error, now, run_id),
            )
            row = self._conn.execute(
                "SELECT * FROM runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            self._conn.commit()
        if row is None:
            raise RuntimeError(f"run not found: {run_id}")
        return _run_from_row(row)

    def append_step(
        self,
        *,
        run_id: str,
        step_kind: StepKind,
        status: StepStatus,
        input_json: dict[str, Any] | None = None,
        output_json: dict[str, Any] | None = None,
        error: str | None = None,
        started_at: str | None = None,
        finished_at: str | None = None,
    ) -> StepRecord:
        """Append one step record to an existing run."""

        start = started_at or utc_now()
        finish = finished_at or start
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(seq), 0) AS seq FROM steps WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            next_seq = int(row["seq"]) + 1 if row is not None else 1
            cursor = self._conn.execute(
                """
                INSERT INTO steps(
                    run_id,
                    seq,
                    step_kind,
                    status,
                    input_json,
                    output_json,
                    error,
                    started_at,
                    finished_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    next_seq,
                    step_kind,
                    status,
                    _dump_json(input_json or {}),
                    _dump_json(output_json or {}),
                    error,
                    start,
                    finish,
                ),
            )
            inserted = self._conn.execute(
                "SELECT * FROM steps WHERE step_id = ?",
                (cursor.lastrowid,),
            ).fetchone()
            self._conn.commit()
        if inserted is None:
            raise RuntimeError("step insert returned no row")
        return _step_from_row(inserted)

    def list_runs(
        self,
        *,
        agent_uri: str | None = None,
        activation_id: str | None = None,
        thread_id: str | None = None,
        origin: MessageOrigin | None = None,
        status: RunStatus | None = None,
        limit: int | None = None,
    ) -> list[RunRecord]:
        """List persisted runs, newest first."""

        clauses: list[str] = []
        params: list[Any] = []
        if agent_uri is not None:
            clauses.append(
                "thread_id IN (SELECT thread_id FROM threads WHERE agent_uri = ?)"
            )
            params.append(agent_uri)
        if activation_id is not None:
            clauses.append("activation_id = ?")
            params.append(activation_id)
        if thread_id is not None:
            clauses.append("thread_id = ?")
            params.append(thread_id)
        if origin is not None:
            clauses.append("origin = ?")
            params.append(origin)
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        limit_sql = ""
        if limit is not None:
            limit_sql = " LIMIT ?"
            params.append(limit)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM runs {where} ORDER BY created_at DESC{limit_sql}",
                params,
            ).fetchall()
        return [_run_from_row(row) for row in rows]

    def list_steps(self, *, run_id: str) -> list[StepRecord]:
        """List persisted steps for one run."""

        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM steps WHERE run_id = ? ORDER BY seq ASC",
                (run_id,),
            ).fetchall()
        return [_step_from_row(row) for row in rows]

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL;")
            self._conn.execute("PRAGMA synchronous=NORMAL;")
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS activations (
                    activation_id TEXT PRIMARY KEY,
                    agent_uri TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    agent_name TEXT NOT NULL,
                    activation_kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    runtime_loops_json TEXT NOT NULL,
                    sandbox TEXT NOT NULL,
                    cap_scopes_json TEXT NOT NULL,
                    sync_fingerprint TEXT,
                    plugin_snapshot_json TEXT NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS threads (
                    thread_id TEXT PRIMARY KEY,
                    agent_uri TEXT NOT NULL,
                    thread_group TEXT NOT NULL,
                    title TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    activation_id TEXT NOT NULL,
                    thread_id TEXT NOT NULL,
                    origin TEXT NOT NULL,
                    channel TEXT,
                    sender TEXT NOT NULL,
                    execution_strategy TEXT NOT NULL,
                    status TEXT NOT NULL,
                    input_text TEXT,
                    output_text TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    FOREIGN KEY(activation_id) REFERENCES activations(activation_id),
                    FOREIGN KEY(thread_id) REFERENCES threads(thread_id)
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS steps (
                    step_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    step_kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    input_json TEXT NOT NULL,
                    output_json TEXT NOT NULL,
                    error TEXT,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    FOREIGN KEY(run_id) REFERENCES runs(run_id)
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS messages (
                    message_id TEXT NOT NULL,
                    thread_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    role TEXT NOT NULL,
                    origin TEXT NOT NULL,
                    channel TEXT,
                    sender TEXT NOT NULL,
                    text TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    meta_json TEXT NOT NULL DEFAULT '{}',
                    parts_json TEXT NOT NULL DEFAULT '[]',
                    FOREIGN KEY(thread_id) REFERENCES threads(thread_id),
                    FOREIGN KEY(run_id) REFERENCES runs(run_id)
                )
                """
            )
            self._conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_execution_activations_agent_started
                ON activations(agent_uri, started_at)
                """
            )
            self._conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_execution_threads_agent_updated
                ON threads(agent_uri, updated_at)
                """
            )
            self._conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_execution_runs_activation_created
                ON runs(activation_id, created_at)
                """
            )
            self._conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_execution_runs_thread_created
                ON runs(thread_id, created_at)
                """
            )
            self._conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_execution_steps_run_seq
                ON steps(run_id, seq)
                """
            )
            self._conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_execution_messages_thread_seq
                ON messages(thread_id, seq)
                """
            )
            self._conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_execution_messages_run_seq
                ON messages(run_id, seq)
                """
            )
            self._conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_execution_messages_id
                ON messages(message_id)
                """
            )
            self._conn.commit()


def _dump_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _load_json(value: str) -> Any:
    return json.loads(value)


def _activation_from_row(row: sqlite3.Row) -> ActivationRecord:
    return ActivationRecord(
        activation_id=str(row["activation_id"]),
        agent_uri=str(row["agent_uri"]),
        agent_id=str(row["agent_id"]),
        agent_name=str(row["agent_name"]),
        activation_kind=row["activation_kind"],
        status=row["status"],
        started_at=str(row["started_at"]),
        finished_at=(
            str(row["finished_at"]) if row["finished_at"] is not None else None
        ),
        runtime_loops=tuple(_load_json(str(row["runtime_loops_json"]))),
        sandbox=str(row["sandbox"]),
        cap_scopes=tuple(_load_json(str(row["cap_scopes_json"]))),
        sync_fingerprint=(
            str(row["sync_fingerprint"])
            if row["sync_fingerprint"] is not None
            else None
        ),
        plugin_snapshot=dict(_load_json(str(row["plugin_snapshot_json"]))),
    )


def _thread_from_row(row: sqlite3.Row) -> ThreadRecord:
    return ThreadRecord(
        thread_id=str(row["thread_id"]),
        agent_uri=str(row["agent_uri"]),
        thread_group=row["thread_group"],
        title=str(row["title"]) if row["title"] is not None else None,
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _run_from_row(row: sqlite3.Row) -> RunRecord:
    return RunRecord(
        run_id=str(row["run_id"]),
        activation_id=str(row["activation_id"]),
        thread_id=str(row["thread_id"]),
        origin=row["origin"],
        channel=str(row["channel"]) if row["channel"] is not None else None,
        sender=row["sender"],
        execution_strategy=row["execution_strategy"],
        status=row["status"],
        input_text=str(row["input_text"]) if row["input_text"] is not None else None,
        output_text=(
            str(row["output_text"]) if row["output_text"] is not None else None
        ),
        error=str(row["error"]) if row["error"] is not None else None,
        created_at=str(row["created_at"]),
        started_at=str(row["started_at"]),
        finished_at=(
            str(row["finished_at"]) if row["finished_at"] is not None else None
        ),
    )


def _step_from_row(row: sqlite3.Row) -> StepRecord:
    return StepRecord(
        step_id=int(row["step_id"]),
        run_id=str(row["run_id"]),
        seq=int(row["seq"]),
        step_kind=row["step_kind"],
        status=row["status"],
        input_json=dict(_load_json(str(row["input_json"]))),
        output_json=dict(_load_json(str(row["output_json"]))),
        error=str(row["error"]) if row["error"] is not None else None,
        started_at=str(row["started_at"]),
        finished_at=str(row["finished_at"]) if row["finished_at"] is not None else None,
    )


def _message_from_row(row: sqlite3.Row) -> RunMessageRecord:
    return RunMessageRecord(
        id=str(row["message_id"]),
        thread_id=str(row["thread_id"]),
        run_id=str(row["run_id"]),
        seq=int(row["seq"]),
        role=row["role"],
        origin=row["origin"],
        channel=str(row["channel"]) if row["channel"] is not None else None,
        sender=row["sender"],
        text=str(row["text"]),
        created_at=str(row["created_at"]),
        meta=dict(_load_json(str(row["meta_json"]))),
        parts=tuple(
            part_from_dict(item)
            for item in _load_json(str(row["parts_json"]))
        ),
    )
