"""SQLite-backed execution truth and persistence sink."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any, cast

from ..base.types.message import Message, MessageRole, Part, parts_from_data, parts_to_data
from .events import RunEnd, RunStart, StepStart, StepEnd, TraceEvent
from .records import (
    ModelCallStepPayload,
    RunRecord,
    RunStatus,
    StepInputItem,
    StepKind,
    StepPayload,
    StepRecord,
    StepStatus,
    UpdateKind,
    UpdateRecord,
    step_input_items_from_data,
    step_input_items_to_data,
    step_payload_from_data,
    step_payload_to_data,
)

_SCHEMA_VERSION = 2


class ExecutionStore:
    """Minimal execution truth store for one agent."""

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

    def start_run(
        self,
        *,
        run_id: str,
        thread_id: str,
        origin: str,
        input: Message,
        created_at: str | None = None,
        started_at: str | None = None,
    ) -> RunRecord:
        created = created_at or utc_now()
        started = started_at or created
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO runs(
                    run_id,
                    thread_id,
                    origin,
                    input,
                    status,
                    error,
                    created_at,
                    started_at,
                    finished_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    thread_id,
                    origin,
                    _dump_json(input.to_data()),
                    "running",
                    None,
                    created,
                    started,
                    None,
                ),
            )
            row = self._conn.execute(
                "SELECT * FROM runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            self._conn.commit()
        if row is None:
            raise RuntimeError("run insert returned no row")
        return _run_from_row(row)

    def finish_run(
        self,
        *,
        run_id: str,
        status: RunStatus = "finished",
        error: str | None = None,
        finished_at: str | None = None,
    ) -> RunRecord:
        now = finished_at or utc_now()
        with self._lock:
            self._conn.execute(
                """
                UPDATE runs
                SET status = ?, error = ?, finished_at = ?
                WHERE run_id = ?
                """,
                (status, error, now, run_id),
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
        return self.finish_run(
            run_id=run_id,
            status="failed",
            error=error,
            finished_at=finished_at,
        )

    def cancel_run(
        self,
        *,
        run_id: str,
        error: str | None = None,
        finished_at: str | None = None,
    ) -> RunRecord:
        return self.finish_run(
            run_id=run_id,
            status="canceled",
            error=error,
            finished_at=finished_at,
        )

    def get_run(self, *, run_id: str) -> RunRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        return _run_from_row(row) if row is not None else None

    def list_runs(
        self,
        *,
        limit: int | None = 50,
        thread_id: str | None = None,
        status: RunStatus | None = None,
    ) -> list[RunRecord]:
        clauses: list[str] = []
        params: list[object] = []
        if thread_id is not None:
            clauses.append("thread_id = ?")
            params.append(thread_id)
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        query = f"SELECT * FROM runs {where} ORDER BY created_at DESC"
        if limit is not None:
            query = f"{query} LIMIT ?"
            params.append(limit)
        with self._lock:
            rows = self._conn.execute(query, tuple(params)).fetchall()
        return [_run_from_row(row) for row in rows]

    def append_step(
        self,
        *,
        run_id: str,
        step_index: int,
        kind: StepKind,
        status: StepStatus,
        input: Sequence[StepInputItem],
        output: Sequence[Part],
        payload: StepPayload,
        error: str | None = None,
        started_at: str,
        finished_at: str,
    ) -> StepRecord:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO steps(
                    run_id,
                    step_index,
                    kind,
                    status,
                    input,
                    output,
                    payload,
                    error,
                    started_at,
                    finished_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    step_index,
                    kind,
                    status,
                    _dump_json(step_input_items_to_data(tuple(input))),
                    _dump_json(parts_to_data(output)),
                    _dump_json(step_payload_to_data(payload)),
                    error,
                    started_at,
                    finished_at,
                ),
            )
            row = self._conn.execute(
                "SELECT * FROM steps WHERE run_id = ? AND step_index = ?",
                (run_id, step_index),
            ).fetchone()
            self._conn.commit()
        if row is None:
            raise RuntimeError("step insert returned no row")
        return _step_from_row(row)

    def list_steps(self, *, run_id: str) -> list[StepRecord]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM steps WHERE run_id = ? ORDER BY step_index ASC",
                (run_id,),
            ).fetchall()
        return [_step_from_row(row) for row in rows]

    def list_steps_for_runs(self, *, run_ids: Sequence[str]) -> dict[str, list[StepRecord]]:
        run_id_list = [item for item in run_ids if item]
        if not run_id_list:
            return {}
        placeholders = ",".join("?" for _ in run_id_list)
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT * FROM steps
                WHERE run_id IN ({placeholders})
                ORDER BY run_id ASC, step_index ASC
                """,
                tuple(run_id_list),
            ).fetchall()
        grouped: dict[str, list[StepRecord]] = {run_id: [] for run_id in run_id_list}
        for row in rows:
            record = _step_from_row(row)
            grouped.setdefault(record.run_id, []).append(record)
        return grouped

    def put_instruction_blob(self, *, body: str) -> str:
        instructions_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
        with self._lock:
            self._conn.execute(
                """
                INSERT OR IGNORE INTO instruction_blobs(
                    instructions_hash,
                    body
                ) VALUES (?, ?)
                """,
                (instructions_hash, body),
            )
            self._conn.commit()
        return instructions_hash

    def get_instruction_blob(self, *, instructions_hash: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT body FROM instruction_blobs
                WHERE instructions_hash = ?
                """,
                (instructions_hash,),
            ).fetchone()
        if row is None:
            return None
        return str(row["body"])

    def recent_conversation_messages(self, *, thread_id: str, limit: int = 20) -> list[Message]:
        runs = sorted(
            self.list_runs(thread_id=thread_id, limit=max(limit, 20)),
            key=lambda item: item.created_at,
        )
        steps_by_run = self.list_steps_for_runs(run_ids=tuple(run.run_id for run in runs))
        results: list[Message] = []
        for run in runs:
            results.append(run.input)
            for step in steps_by_run.get(run.run_id, ()):
                results.extend(_replay_messages_from_step(step))
        return results[-limit:]

    def append_update(
        self,
        *,
        kind: UpdateKind,
        payload: dict[str, Any] | None = None,
        created_at: str | None = None,
    ) -> UpdateRecord:
        now = created_at or utc_now()
        with self._lock:
            cursor = self._conn.execute(
                """
                INSERT INTO updates(
                    kind,
                    payload,
                    created_at
                ) VALUES (?, ?, ?)
                """,
                (
                    kind,
                    _dump_json(payload or {}),
                    now,
                ),
            )
            inserted = self._conn.execute(
                "SELECT * FROM updates WHERE update_id = ?",
                (cursor.lastrowid,),
            ).fetchone()
            self._conn.commit()
        if inserted is None:
            raise RuntimeError("update insert returned no row")
        return _update_from_row(inserted)

    def list_updates(self, *, limit: int = 100) -> list[UpdateRecord]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM updates ORDER BY update_id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [_update_from_row(row) for row in reversed(rows)]

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL;")
            self._conn.execute("PRAGMA synchronous=NORMAL;")
            version = int(self._conn.execute("PRAGMA user_version").fetchone()[0])
            if version != _SCHEMA_VERSION:
                self._conn.execute("DROP TABLE IF EXISTS steps")
                self._conn.execute("DROP TABLE IF EXISTS runs")
                self._conn.execute("DROP TABLE IF EXISTS updates")
                self._conn.execute("DROP TABLE IF EXISTS instruction_blobs")
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    thread_id TEXT NOT NULL,
                    origin TEXT NOT NULL,
                    input TEXT NOT NULL,
                    status TEXT NOT NULL,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS steps (
                    run_id TEXT NOT NULL,
                    step_index INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    input TEXT NOT NULL,
                    output TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    error TEXT,
                    started_at TEXT NOT NULL,
                    finished_at TEXT NOT NULL,
                    PRIMARY KEY(run_id, step_index),
                    FOREIGN KEY(run_id) REFERENCES runs(run_id)
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS updates (
                    update_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS instruction_blobs (
                    instructions_hash TEXT PRIMARY KEY,
                    body TEXT NOT NULL
                )
                """
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_runs_thread_created ON runs(thread_id, created_at)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_steps_run_step_index ON steps(run_id, step_index)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_updates_created ON updates(created_at)"
            )
            self._conn.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")
            self._conn.commit()


def execution_db_path(toolang_root: Path, agent_name: str) -> Path:
    return toolang_root / "agents" / agent_name / ".runtime" / "execution.db"


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _dump_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _load_json(value: str) -> Any:
    return json.loads(value)


def _run_from_row(row: sqlite3.Row) -> RunRecord:
    input_raw = _load_json(str(row["input"]))
    return RunRecord(
        run_id=str(row["run_id"]),
        thread_id=str(row["thread_id"]),
        origin=str(row["origin"]),
        input=Message.from_data(input_raw if isinstance(input_raw, Mapping) else {}),
        status=cast(RunStatus, row["status"]),
        error=str(row["error"]) if row["error"] is not None else None,
        created_at=str(row["created_at"]),
        started_at=str(row["started_at"]),
        finished_at=str(row["finished_at"]) if row["finished_at"] is not None else None,
    )


def _step_from_row(row: sqlite3.Row) -> StepRecord:
    raw = dict(row)
    input_raw = _load_json(str(raw["input"]))
    output_raw = _load_json(str(raw["output"]))
    payload_raw = _load_json(str(raw["payload"]))
    input_items = (
        input_raw
        if isinstance(input_raw, Sequence) and not isinstance(input_raw, (str, bytes, bytearray))
        else []
    )
    output_items = (
        output_raw
        if isinstance(output_raw, Sequence) and not isinstance(output_raw, (str, bytes, bytearray))
        else []
    )
    return StepRecord(
        run_id=str(raw["run_id"]),
        step_index=int(cast(int | str, raw["step_index"])),
        kind=cast(StepKind, raw["kind"]),
        status=cast(StepStatus, raw["status"]),
        input=step_input_items_from_data(
            [item for item in input_items if isinstance(item, Mapping)]
        ),
        output=parts_from_data(
            [item for item in output_items if isinstance(item, Mapping)]
        ),
        started_at=str(raw["started_at"]),
        finished_at=str(raw["finished_at"]),
        payload=step_payload_from_data(
            cast(StepKind, raw["kind"]),
            payload_raw if isinstance(payload_raw, Mapping) else {},
        ),
        error=str(raw["error"]) if raw["error"] is not None else None,
    )


def _update_from_row(row: sqlite3.Row) -> UpdateRecord:
    payload_raw = _load_json(str(row["payload"]))
    return UpdateRecord(
        update_id=int(row["update_id"]),
        kind=cast(UpdateKind, row["kind"]),
        payload=dict(payload_raw) if isinstance(payload_raw, Mapping) else {},
        created_at=str(row["created_at"]),
    )


def _replay_messages_from_step(step: StepRecord) -> list[Message]:
    role = _role_for_step(step.kind)
    if role is None or not step.output:
        return []
    meta: dict[str, Any] = {}
    if step.error is not None:
        meta["error"] = step.error
    return [Message(role=role, parts=tuple(step.output), meta=meta)]


def _role_for_step(kind: StepKind) -> MessageRole | None:
    if kind == "model_call":
        return "assistant"
    if kind == "tool_call":
        return "tool"
    return None


class PersistSink:
    """Persist trace events into the execution store."""

    def __init__(self, store: ExecutionStore) -> None:
        self._store = store
        self._pending_steps: dict[tuple[str, int], tuple[tuple[StepInputItem, ...], str, str | None]] = {}

    def on_event(self, event: TraceEvent) -> None:
        if isinstance(event, RunStart):
            self._store.start_run(
                run_id=event.run_id,
                thread_id=event.thread_id,
                origin=event.origin,
                input=event.input,
                created_at=event.created_at,
                started_at=event.started_at,
            )
            return
        if isinstance(event, StepStart):
            instructions_hash = (
                self._store.put_instruction_blob(body=event.instructions)
                if event.instructions is not None
                else None
            )
            self._pending_steps[(event.run_id, event.step_index)] = (
                tuple(event.input),
                event.started_at,
                instructions_hash,
            )
            return
        if isinstance(event, StepEnd):
            step_input, started_at, instructions_hash = self._pending_steps.pop(
                (event.run_id, event.step_index),
                ((), event.started_at, None),
            )
            payload = event.payload
            if isinstance(payload, ModelCallStepPayload):
                payload = ModelCallStepPayload(
                    model_ref=payload.model_ref,
                    input_tokens=payload.input_tokens,
                    output_tokens=payload.output_tokens,
                    instructions_hash=instructions_hash,
                )
            self._store.append_step(
                run_id=event.run_id,
                step_index=event.step_index,
                kind=event.kind,
                status=event.status,
                input=step_input,
                output=event.output,
                payload=payload,
                error=event.error,
                started_at=started_at,
                finished_at=event.finished_at,
            )
            return
        if isinstance(event, RunEnd):
            self._store.finish_run(
                run_id=event.run_id,
                status=event.status,
                error=event.error,
                finished_at=event.finished_at,
            )
