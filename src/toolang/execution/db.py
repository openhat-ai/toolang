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

from toolang.base.types.message import (
    Message,
    MessageRole,
    Part,
    TextPart,
    ToolCallPart,
    ToolResultPart,
    parts_from_data,
    parts_to_data,
)
from .events import RunEnd, RunBegin, StepBegin, StepEnd, TraceEvent
from .records import (
    EventDomain,
    EventRecord,
    CommandKind,
    CommandMode,
    CommandRecord,
    ModelCallStepPayload,
    RunRecord,
    RunStatus,
    RuntimeStepPayload,
    StepInputItem,
    StepKind,
    StepPayload,
    StepRecord,
    StepStatus,
    ThreadPeer,
    ThreadRecord,
    UpdateKind,
    UpdateRecord,
    step_input_items_from_data,
    step_input_items_to_data,
    step_payload_from_data,
    step_payload_to_data,
)

_SCHEMA_VERSION = 9


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
        root_run_id: str | None = None,
        parent_run_id: str | None = None,
        parent_step_index: int | None = None,
        executable_kind: str = "thunk",
        executable_name: str | None = None,
        call_kind: str = "top",
        metadata: Mapping[str, Any] | None = None,
        request_id: str | None = None,
        created_at: str | None = None,
        started_at: str | None = None,
    ) -> RunRecord:
        created = created_at or utc_now()
        started = started_at or created
        with self._lock:
            self._ensure_thread_locked(
                thread_id=thread_id,
                origin=origin,
                peer=None,
                parent=None,
                created_at=created,
                updated_at=started,
            )
            self._conn.execute(
                """
                INSERT INTO runs(
                    run_id,
                    thread_id,
                    origin,
                    root_run_id,
                    parent_run_id,
                    parent_step_index,
                    executable_kind,
                    executable_name,
                    call_kind,
                    metadata,
                    status,
                    error,
                    superseded,
                    created_at,
                    started_at,
                    finished_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    thread_id,
                    origin,
                    root_run_id or run_id,
                    parent_run_id,
                    parent_step_index,
                    executable_kind,
                    executable_name,
                    call_kind,
                    _dump_json(dict(metadata or {})),
                    "running",
                    None,
                    None,
                    created,
                    started,
                    None,
                ),
            )
            self._conn.execute(
                """
                    INSERT INTO commands(
                        run_id,
                        "index",
                        kind,
                        mode,
                        request_id,
                        message,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    0,
                    "start",
                    None,
                    request_id,
                    _dump_json(input.to_data()),
                    created,
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

    def ensure_thread(
        self,
        *,
        thread_id: str,
        origin: str = "chat",
        peer: ThreadPeer | None = None,
        parent: str | None = None,
        created_at: str | None = None,
        updated_at: str | None = None,
    ) -> ThreadRecord:
        """Create one thread metadata row if needed and validate supplied metadata."""

        now = updated_at or created_at or utc_now()
        with self._lock:
            record = self._ensure_thread_locked(
                thread_id=thread_id,
                origin=origin,
                peer=peer,
                parent=parent,
                created_at=created_at or now,
                updated_at=now,
            )
            self._conn.commit()
            return record

    def get_thread(self, *, thread_id: str) -> ThreadRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM threads WHERE thread_id = ?",
                (thread_id,),
            ).fetchone()
        return _thread_from_row(row) if row is not None else None

    def list_threads(self) -> list[ThreadRecord]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM threads ORDER BY updated_at DESC, created_at DESC",
            ).fetchall()
        return [_thread_from_row(row) for row in rows]

    def update_thread_peer(
        self,
        *,
        thread_id: str,
        peer: ThreadPeer,
        updated_at: str | None = None,
    ) -> ThreadRecord:
        now = updated_at or utc_now()
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM threads WHERE thread_id = ?",
                (thread_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"thread not found: {thread_id}")
            self._conn.execute(
                """
                UPDATE threads
                SET peer = ?, updated_at = ?
                WHERE thread_id = ?
                """,
                (_dump_json(peer.to_data()), now, thread_id),
            )
            updated = self._conn.execute(
                "SELECT * FROM threads WHERE thread_id = ?",
                (thread_id,),
            ).fetchone()
            self._conn.commit()
        if updated is None:
            raise RuntimeError(f"thread not found after update: {thread_id}")
        return _thread_from_row(updated)

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

    def supersede_run(
        self,
        *,
        run_id: str,
        superseded: Mapping[str, object],
    ) -> RunRecord:
        with self._lock:
            self._conn.execute(
                "UPDATE runs SET superseded = ? WHERE run_id = ?",
                (_dump_json(dict(superseded)), run_id),
            )
            row = self._conn.execute(
                "SELECT * FROM runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            self._conn.commit()
        if row is None:
            raise RuntimeError(f"run not found: {run_id}")
        return _run_from_row(row)

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
        include_superseded: bool = False,
    ) -> list[RunRecord]:
        clauses: list[str] = []
        params: list[object] = []
        if thread_id is not None:
            clauses.append("thread_id = ?")
            params.append(thread_id)
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        if not include_superseded:
            clauses.append("superseded IS NULL")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        query = f"SELECT * FROM runs {where} ORDER BY created_at DESC"
        if limit is not None:
            query = f"{query} LIMIT ?"
            params.append(limit)
        with self._lock:
            rows = self._conn.execute(query, tuple(params)).fetchall()
        return [_run_from_row(row) for row in rows]

    def active_run_for_thread(self, *, thread_id: str) -> RunRecord | None:
        """Return the currently running run for one thread, if any."""

        runs = self.list_runs(thread_id=thread_id, status="running", limit=1)
        return runs[0] if runs else None

    def list_thread_runs_chronological(
        self,
        *,
        thread_id: str,
        limit: int | None = None,
        include_superseded: bool = False,
    ) -> tuple[RunRecord, ...]:
        """Return one thread's runs in durable chronological order."""

        clauses = ["thread_id = ?"]
        params: list[object] = [thread_id]
        if not include_superseded:
            clauses.append("superseded IS NULL")
        query = f"""
            SELECT * FROM runs
            WHERE {' AND '.join(clauses)}
            ORDER BY created_at ASC, rowid ASC
        """
        if limit is not None:
            query = f"{query} LIMIT ?"
            params.append(limit)
        with self._lock:
            rows = self._conn.execute(query, tuple(params)).fetchall()
        return tuple(_run_from_row(row) for row in rows)

    def list_thread_runs_before(self, *, run_id: str) -> tuple[RunRecord, ...]:
        """Return runs in the same thread before one anchor run."""

        with self._lock:
            anchor = self._conn.execute(
                "SELECT rowid, * FROM runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if anchor is None:
                raise ValueError(f"run not found: {run_id}")
            rows = self._conn.execute(
                """
                SELECT * FROM runs
                WHERE thread_id = ?
                  AND superseded IS NULL
                  AND (
                    created_at < ?
                    OR (created_at = ? AND rowid < ?)
                  )
                ORDER BY created_at ASC, rowid ASC
                """,
                (anchor["thread_id"], anchor["created_at"], anchor["created_at"], anchor["rowid"]),
            ).fetchall()
        return tuple(_run_from_row(row) for row in rows)

    def copy_runs_to_thread(
        self,
        *,
        source_run_ids: Sequence[str],
        target_thread_id: str,
        target_run_ids: Sequence[str],
    ) -> tuple[RunRecord, ...]:
        """Copy durable run records, commands, and steps into another thread."""

        if len(source_run_ids) != len(target_run_ids):
            raise ValueError("source and target run id counts must match")
        if not source_run_ids:
            return ()
        run_id_map = dict(zip(source_run_ids, target_run_ids, strict=True))
        with self._lock:
            copied: list[sqlite3.Row] = []
            for source_run_id, target_run_id in zip(source_run_ids, target_run_ids, strict=True):
                source_run = self._conn.execute(
                    "SELECT * FROM runs WHERE run_id = ?",
                    (source_run_id,),
                ).fetchone()
                if source_run is None:
                    raise ValueError(f"run not found: {source_run_id}")
                self._conn.execute(
                    """
                    INSERT INTO runs(
                        run_id,
                        thread_id,
                        origin,
                        root_run_id,
                        parent_run_id,
                        parent_step_index,
                        executable_kind,
                        executable_name,
                        call_kind,
                        metadata,
                        status,
                        error,
                        superseded,
                        created_at,
                        started_at,
                        finished_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        target_run_id,
                        target_thread_id,
                        source_run["origin"],
                        run_id_map.get(source_run["root_run_id"], target_run_id),
                        (
                            run_id_map.get(source_run["parent_run_id"])
                            if source_run["parent_run_id"] is not None
                            else None
                        ),
                        source_run["parent_step_index"],
                        source_run["executable_kind"],
                        source_run["executable_name"],
                        source_run["call_kind"],
                        source_run["metadata"],
                        source_run["status"],
                        source_run["error"],
                        source_run["superseded"],
                        source_run["created_at"],
                        source_run["started_at"],
                        source_run["finished_at"],
                    ),
                )
                command_rows = self._conn.execute(
                    """
                    SELECT * FROM commands
                    WHERE run_id = ?
                    ORDER BY "index" ASC
                    """,
                    (source_run_id,),
                ).fetchall()
                self._conn.executemany(
                    """
                    INSERT INTO commands(
                        run_id,
                        "index",
                        kind,
                        mode,
                        request_id,
                        message,
                        created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            target_run_id,
                            row["index"],
                            row["kind"],
                            row["mode"],
                            row["request_id"],
                            row["message"],
                            row["created_at"],
                        )
                        for row in command_rows
                    ],
                )
                step_rows = self._conn.execute(
                    """
                    SELECT * FROM steps
                    WHERE run_id = ?
                    ORDER BY step_index ASC
                    """,
                    (source_run_id,),
                ).fetchall()
                self._conn.executemany(
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
                    [
                        (
                            target_run_id,
                            row["step_index"],
                            row["kind"],
                            row["status"],
                            row["input"],
                            row["output"],
                            row["payload"],
                            row["error"],
                            row["started_at"],
                            row["finished_at"],
                        )
                        for row in step_rows
                    ],
                )
                inserted = self._conn.execute(
                    "SELECT * FROM runs WHERE run_id = ?",
                    (target_run_id,),
                ).fetchone()
                if inserted is None:
                    raise RuntimeError("copied run insert returned no row")
                copied.append(inserted)
            self._conn.commit()
        return tuple(_run_from_row(row) for row in copied)

    def supersede_thread_from_run(
        self,
        *,
        run_id: str,
        superseded: Mapping[str, object],
    ) -> tuple[RunRecord, ...]:
        """Mark one run and later runs in the same thread as superseded."""

        anchor = self.get_run(run_id=run_id)
        if anchor is None:
            raise ValueError(f"run not found: {run_id}")
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM runs
                WHERE thread_id = ?
                  AND superseded IS NULL
                  AND created_at >= ?
                ORDER BY created_at
                """,
                (anchor.thread_id, anchor.created_at),
            ).fetchall()
            self._conn.executemany(
                "UPDATE runs SET superseded = ? WHERE run_id = ?",
                [(_dump_json(dict(superseded)), str(row["run_id"])) for row in rows],
            )
            updated = self._conn.execute(
                """
                SELECT * FROM runs
                WHERE thread_id = ?
                  AND superseded IS NOT NULL
                  AND created_at >= ?
                ORDER BY created_at
                """,
                (anchor.thread_id, anchor.created_at),
            ).fetchall()
            self._conn.commit()
        return tuple(_run_from_row(row) for row in updated)

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

    def put_prompt(self, *, body: str) -> str:
        prompt_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
        with self._lock:
            self._conn.execute(
                """
                INSERT OR IGNORE INTO prompts(
                    hash,
                    body
                ) VALUES (?, ?)
                """,
                (prompt_hash, body),
            )
            self._conn.commit()
        return prompt_hash

    def get_prompt(self, *, prompt_hash: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT body FROM prompts
                WHERE hash = ?
                """,
                (prompt_hash,),
            ).fetchone()
        if row is None:
            return None
        return str(row["body"])

    def recent_conversation_messages(self, *, thread_id: str, limit: int = 20) -> list[Message]:
        runs = self._conversation_runs(thread_id=thread_id, limit=max(limit, 20))
        steps_by_run = self.list_steps_for_runs(run_ids=tuple(run.run_id for run in runs))
        results: list[Message] = []
        for run in runs:
            inputs = self.list_commands(run_id=run.run_id)
            if inputs:
                results.extend(item.message for item in inputs if item.message is not None)
            for step in steps_by_run.get(run.run_id, ()):
                results.extend(_replay_messages_from_step(step))
        return _recent_valid_model_history(results, limit=limit)

    def recent_text_conversation_messages(
        self,
        *,
        thread_id: str,
        limit: int = 32,
    ) -> list[Message]:
        """Return recent actor messages without raw tool-call or tool-result parts."""

        runs = self._conversation_runs(thread_id=thread_id, limit=max(limit * 4, 100))
        steps_by_run = self.list_steps_for_runs(run_ids=tuple(run.run_id for run in runs))
        results: list[Message] = []
        for run in runs:
            inputs = self.list_commands(run_id=run.run_id)
            input_messages = [item.message for item in inputs if item.message is not None]
            for input_message in input_messages:
                actor_message = _actor_text_message(input_message)
                if actor_message is not None:
                    results.append(actor_message)
            for step in steps_by_run.get(run.run_id, ()):
                for message in _replay_messages_from_step(step):
                    actor_message = _actor_text_message(message)
                    if actor_message is not None:
                        results.append(actor_message)
        return results[-limit:]

    def _conversation_runs(self, *, thread_id: str, limit: int) -> list[RunRecord]:
        current = list(self.list_thread_runs_chronological(thread_id=thread_id, limit=None))
        return current[-limit:]

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

    def append_event(
        self,
        *,
        domain: EventDomain,
        domain_id: str,
        type: str,
        payload: dict[str, Any] | None = None,
        created_at: str | None = None,
    ) -> EventRecord:
        """Append one resource-scoped event and return it with its domain cursor."""

        now = created_at or utc_now()
        with self._lock:
            seq_row = self._conn.execute(
                """
                SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq
                FROM events
                WHERE domain = ? AND domain_id = ?
                """,
                (domain, domain_id),
            ).fetchone()
            seq = int(seq_row["next_seq"]) if seq_row is not None else 1
            inserted = self._conn.execute(
                """
                INSERT INTO events(
                    domain,
                    domain_id,
                    seq,
                    type,
                    payload,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    domain,
                    domain_id,
                    seq,
                    type,
                    _dump_json(payload or {}),
                    now,
                ),
            )
            row = self._conn.execute(
                "SELECT * FROM events WHERE event_id = ?",
                (inserted.lastrowid,),
            ).fetchone()
            self._conn.commit()
        if row is None:
            raise RuntimeError("event insert returned no row")
        return _event_from_row(row)

    def list_events(
        self,
        *,
        domain: EventDomain,
        domain_id: str,
        after: int | None = None,
        limit: int = 100,
    ) -> list[EventRecord]:
        clauses = ["domain = ?", "domain_id = ?"]
        params: list[object] = [domain, domain_id]
        if after is not None:
            clauses.append("seq > ?")
            params.append(after)
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT * FROM events
                WHERE {' AND '.join(clauses)}
                ORDER BY seq ASC
                LIMIT ?
                """,
                tuple(params),
            ).fetchall()
        return [_event_from_row(row) for row in rows]

    def latest_event_cursor(self, *, domain: EventDomain, domain_id: str) -> int:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT COALESCE(MAX(seq), 0) AS seq
                FROM events
                WHERE domain = ? AND domain_id = ?
                """,
                (domain, domain_id),
            ).fetchone()
        return int(row["seq"]) if row is not None else 0

    def append_command(
        self,
        *,
        run_id: str,
        kind: CommandKind,
        mode: CommandMode | None = None,
        request_id: str | None = None,
        message: Message | None = None,
        created_at: str | None = None,
    ) -> CommandRecord:
        """Append one client-side input to one run."""

        now = created_at or utc_now()
        with self._lock:
            index_row = self._conn.execute(
                """
                SELECT COALESCE(MAX("index"), -1) + 1 AS next_index
                FROM commands
                WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()
            index = int(index_row["next_index"]) if index_row is not None else 0
            self._conn.execute(
                """
                INSERT INTO commands(
                    run_id,
                    "index",
                    kind,
                    mode,
                    request_id,
                    message,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    index,
                    kind,
                    mode,
                    request_id,
                    _dump_json(message.to_data()) if message is not None else None,
                    now,
                ),
            )
            row = self._conn.execute(
                'SELECT * FROM commands WHERE run_id = ? AND "index" = ?',
                (run_id, index),
            ).fetchone()
            self._conn.commit()
        if row is None:
            raise RuntimeError("command insert returned no row")
        return _command_from_row(row)

    def get_command(self, *, run_id: str, index: int) -> CommandRecord | None:
        with self._lock:
            row = self._conn.execute(
                'SELECT * FROM commands WHERE run_id = ? AND "index" = ?',
                (run_id, index),
            ).fetchone()
        return _command_from_row(row) if row is not None else None

    def list_commands(
        self,
        *,
        run_id: str,
        kind: CommandKind | None = None,
    ) -> tuple[CommandRecord, ...]:
        clauses = ["run_id = ?"]
        params: list[object] = [run_id]
        if kind is not None:
            clauses.append("kind = ?")
            params.append(kind)
        where = " AND ".join(clauses)
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT * FROM commands
                WHERE {where}
                ORDER BY "index" ASC
                """,
                tuple(params),
            ).fetchall()
        return tuple(_command_from_row(row) for row in rows)

    def pending_commands(
        self,
        *,
        run_id: str,
        kind: CommandKind,
    ) -> tuple[CommandRecord, ...]:
        """Return run commands not yet referenced by any step input."""

        with self._lock:
            command_rows = self._conn.execute(
                """
                SELECT * FROM commands
                WHERE run_id = ? AND kind = ?
                ORDER BY "index" ASC
                """,
                (run_id, kind),
            ).fetchall()
            step_rows = self._conn.execute(
                """
                SELECT input FROM steps
                WHERE run_id = ?
                """,
                (run_id,),
            ).fetchall()
        used = _used_command_indexes(step_rows)
        return tuple(_command_from_row(row) for row in command_rows if int(row["index"]) not in used)

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL;")
            self._conn.execute("PRAGMA synchronous=NORMAL;")
            version = int(self._conn.execute("PRAGMA user_version").fetchone()[0])
            if version != _SCHEMA_VERSION:
                self._conn.execute("DROP TABLE IF EXISTS steps")
                self._conn.execute("DROP TABLE IF EXISTS runs")
                self._conn.execute("DROP TABLE IF EXISTS inputs")
                self._conn.execute("DROP TABLE IF EXISTS commands")
                self._conn.execute("DROP TABLE IF EXISTS threads")
                self._conn.execute("DROP TABLE IF EXISTS updates")
                self._conn.execute("DROP TABLE IF EXISTS events")
                self._conn.execute("DROP TABLE IF EXISTS instruction_blobs")
                self._conn.execute("DROP TABLE IF EXISTS prompts")
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS threads (
                    thread_id TEXT PRIMARY KEY,
                    origin TEXT NOT NULL,
                    peer TEXT NOT NULL,
                    parent TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    thread_id TEXT NOT NULL,
                    origin TEXT NOT NULL,
                    root_run_id TEXT NOT NULL,
                    parent_run_id TEXT,
                    parent_step_index INTEGER,
                    executable_kind TEXT NOT NULL,
                    executable_name TEXT,
                    call_kind TEXT NOT NULL,
                    metadata TEXT NOT NULL,
                    status TEXT NOT NULL,
                    error TEXT,
                    superseded TEXT,
                    created_at TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS commands (
                    run_id TEXT NOT NULL,
                    "index" INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    mode TEXT,
                    request_id TEXT,
                    message TEXT,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(run_id, "index"),
                    FOREIGN KEY(run_id) REFERENCES runs(run_id)
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
                CREATE TABLE IF NOT EXISTS events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    domain TEXT NOT NULL,
                    domain_id TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    type TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(domain, domain_id, seq)
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS prompts (
                    hash TEXT PRIMARY KEY,
                    body TEXT NOT NULL
                )
                """
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_runs_thread_created ON runs(thread_id, created_at)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_threads_updated ON threads(updated_at)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_steps_run_step_index ON steps(run_id, step_index)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_updates_created ON updates(created_at)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_commands_request_id ON commands(request_id) WHERE request_id IS NOT NULL"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_events_domain_seq ON events(domain, domain_id, seq)"
            )
            self._conn.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")
            self._conn.commit()

    def _ensure_thread_locked(
        self,
        *,
        thread_id: str,
        origin: str,
        peer: ThreadPeer | None,
        parent: str | None,
        created_at: str,
        updated_at: str,
    ) -> ThreadRecord:
        row = self._conn.execute(
            "SELECT * FROM threads WHERE thread_id = ?",
            (thread_id,),
        ).fetchone()
        if row is not None:
            record = _thread_from_row(row)
            if peer is not None and record.peer != peer:
                raise ValueError(f"thread peer mismatch: {thread_id}")
            if parent is not None and record.parent not in {None, parent}:
                raise ValueError(f"thread parent mismatch: {thread_id}")
            if parent is not None and record.parent is None:
                self._conn.execute(
                    "UPDATE threads SET parent = ?, updated_at = ? WHERE thread_id = ?",
                    (parent, updated_at, thread_id),
                )
                updated = self._conn.execute(
                    "SELECT * FROM threads WHERE thread_id = ?",
                    (thread_id,),
                ).fetchone()
                if updated is None:
                    raise RuntimeError(f"thread not found after parent update: {thread_id}")
                return _thread_from_row(updated)
            return record
        effective_peer = peer or ThreadPeer()
        self._conn.execute(
            """
            INSERT INTO threads(
                thread_id,
                origin,
                peer,
                parent,
                created_at,
                updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                thread_id,
                origin,
                _dump_json(effective_peer.to_data()),
                parent,
                created_at,
                updated_at,
            ),
        )
        inserted = self._conn.execute(
            "SELECT * FROM threads WHERE thread_id = ?",
            (thread_id,),
        ).fetchone()
        if inserted is None:
            raise RuntimeError("thread insert returned no row")
        return _thread_from_row(inserted)


def execution_db_path(toolang_root: Path, agent_name: str) -> Path:
    return toolang_root / "agents" / agent_name / ".runtime" / "runs.db"


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _dump_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _load_json(value: str) -> Any:
    return json.loads(value)


def _run_from_row(row: sqlite3.Row) -> RunRecord:
    raw_superseded = row["superseded"] if "superseded" in row.keys() else None
    return RunRecord(
        run_id=str(row["run_id"]),
        thread_id=str(row["thread_id"]),
        origin=str(row["origin"]),
        root_run_id=str(row["root_run_id"]) if "root_run_id" in row.keys() else str(row["run_id"]),
        parent_run_id=(
            str(row["parent_run_id"])
            if "parent_run_id" in row.keys() and row["parent_run_id"] is not None
            else None
        ),
        parent_step_index=(
            int(row["parent_step_index"])
            if "parent_step_index" in row.keys() and row["parent_step_index"] is not None
            else None
        ),
        executable_kind=(
            str(row["executable_kind"])
            if "executable_kind" in row.keys()
            else "thunk"
        ),
        executable_name=(
            str(row["executable_name"])
            if "executable_name" in row.keys() and row["executable_name"] is not None
            else None
        ),
        call_kind=(
            str(row["call_kind"])
            if "call_kind" in row.keys()
            else "top"
        ),
        metadata=(
            cast(dict[str, Any], _load_json(str(row["metadata"])))
            if "metadata" in row.keys() and row["metadata"] is not None
            else {}
        ),
        status=cast(RunStatus, row["status"]),
        error=str(row["error"]) if row["error"] is not None else None,
        superseded=cast(dict[str, Any], _load_json(str(raw_superseded))) if raw_superseded is not None else None,
        created_at=str(row["created_at"]),
        started_at=str(row["started_at"]),
        finished_at=str(row["finished_at"]) if row["finished_at"] is not None else None,
    )


def _thread_from_row(row: sqlite3.Row) -> ThreadRecord:
    raw = dict(row)
    peer_raw = _load_json(str(raw["peer"]))
    return ThreadRecord(
        thread_id=str(raw["thread_id"]),
        origin=str(raw["origin"]),
        peer=ThreadPeer.from_data(peer_raw if isinstance(peer_raw, Mapping) else None),
        parent=str(raw["parent"]) if raw["parent"] is not None else None,
        created_at=str(raw["created_at"]),
        updated_at=str(raw["updated_at"]),
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


def _event_from_row(row: sqlite3.Row) -> EventRecord:
    payload_raw = _load_json(str(row["payload"]))
    return EventRecord(
        event_id=int(row["event_id"]),
        domain=cast(EventDomain, row["domain"]),
        domain_id=str(row["domain_id"]),
        seq=int(row["seq"]),
        type=str(row["type"]),
        payload=dict(payload_raw) if isinstance(payload_raw, Mapping) else {},
        created_at=str(row["created_at"]),
    )


def _command_from_row(row: sqlite3.Row) -> CommandRecord:
    message_raw = _load_json(str(row["message"])) if row["message"] is not None else None
    return CommandRecord(
        run_id=str(row["run_id"]),
        index=int(row["index"]),
        kind=cast(CommandKind, row["kind"]),
        mode=cast(CommandMode, row["mode"]) if row["mode"] is not None else None,
        request_id=str(row["request_id"]) if row["request_id"] is not None else None,
        message=Message.from_data(message_raw) if isinstance(message_raw, Mapping) else None,
        created_at=str(row["created_at"]),
    )


def _used_command_indexes(rows: Sequence[sqlite3.Row]) -> set[int]:
    used: set[int] = set()
    for row in rows:
        raw = _load_json(str(row["input"]))
        if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes, bytearray)):
            continue
        for item in raw:
            if not isinstance(item, Mapping):
                continue
            if item.get("kind") == "command":
                used.add(int(item.get("index", 0)))
    return used


def _replay_messages_from_step(step: StepRecord) -> list[Message]:
    role = _role_for_step(step.kind)
    if role is None or not step.output:
        return []
    meta: dict[str, Any] = {}
    if step.error is not None:
        meta["error"] = step.error
    if isinstance(step.payload, ModelCallStepPayload) and step.payload.reasoning_content:
        meta["reasoning_content"] = step.payload.reasoning_content
    return [Message(role=role, parts=tuple(step.output), meta=meta)]


def _role_for_step(kind: StepKind) -> MessageRole | None:
    if kind == "model":
        return "assistant"
    if kind == "tool":
        return "tool"
    return None


def _actor_text_message(message: Message) -> Message | None:
    if message.role not in {"user", "assistant"}:
        return None
    parts = tuple(
        part
        for part in message.parts
        if not isinstance(part, (ToolCallPart, ToolResultPart))
    )
    if not parts:
        return None
    return Message(role=message.role, parts=parts, meta=dict(message.meta))


def _recent_valid_model_history(messages: Sequence[Message], *, limit: int) -> list[Message]:
    if limit <= 0:
        return []
    groups = _valid_model_history_groups(messages)
    selected: list[tuple[Message, ...]] = []
    count = 0
    for group in reversed(groups):
        group_size = len(group)
        if selected and count + group_size > limit:
            break
        selected.append(group)
        count += group_size
        if count >= limit:
            break
    selected.reverse()
    return [message for group in selected for message in group]


def _valid_model_history_groups(messages: Sequence[Message]) -> list[tuple[Message, ...]]:
    groups: list[tuple[Message, ...]] = []
    index = 0
    while index < len(messages):
        message = messages[index]
        if message.role == "tool":
            index += 1
            continue
        if message.role != "assistant":
            groups.append((message,))
            index += 1
            continue
        tool_call_ids = _message_tool_call_ids(message)
        if not tool_call_ids:
            groups.append((message,))
            index += 1
            continue
        tool_group: list[Message] = []
        remaining = set(tool_call_ids)
        cursor = index + 1
        while cursor < len(messages) and messages[cursor].role == "tool":
            tool_message = messages[cursor]
            matched = _message_tool_result_ids(tool_message) & remaining
            if matched:
                tool_group.append(tool_message)
                remaining -= matched
            cursor += 1
        if not remaining:
            groups.append((message, *tool_group))
        index = cursor
    return groups


def _message_tool_call_ids(message: Message) -> tuple[str, ...]:
    return tuple(part.tool_call_id for part in message.parts if isinstance(part, ToolCallPart) and part.tool_call_id)


def _message_tool_result_ids(message: Message) -> set[str]:
    return {part.tool_call_id for part in message.parts if isinstance(part, ToolResultPart) and part.tool_call_id}


class PersistSink:
    """Persist trace events into the execution store."""

    def __init__(self, store: ExecutionStore) -> None:
        self._store = store
        self._pending_steps: dict[tuple[str, int], tuple[tuple[StepInputItem, ...], str, str | None, str | None]] = {}
        self._last_step_index: dict[str, int] = {}

    def on_event(self, event: TraceEvent) -> None:
        if isinstance(event, RunBegin):
            self._store.start_run(
                run_id=event.run_id,
                thread_id=event.thread_id,
                origin=event.origin,
                input=event.input,
                root_run_id=event.root_run_id,
                parent_run_id=event.parent_run_id,
                parent_step_index=event.parent_step_index,
                executable_kind=event.executable_kind,
                executable_name=event.executable_name,
                call_kind=event.call_kind,
                metadata=event.metadata,
                request_id=event.request_id,
                created_at=event.created_at,
                started_at=event.started_at,
            )
            return
        if isinstance(event, StepBegin):
            instruct = (
                self._store.put_prompt(body=event.instruct)
                if event.instruct is not None
                else None
            )
            context = (
                self._store.put_prompt(body=event.context)
                if event.context is not None
                else None
            )
            self._pending_steps[(event.run_id, event.step_index)] = (
                tuple(event.input),
                event.started_at,
                instruct,
                context,
            )
            return
        if isinstance(event, StepEnd):
            step_input, started_at, instruct, context = self._pending_steps.pop(
                (event.run_id, event.step_index),
                ((), event.started_at, None, None),
            )
            payload = event.payload
            if isinstance(payload, ModelCallStepPayload):
                payload = ModelCallStepPayload(
                    model_ref=payload.model_ref,
                    input_tokens=payload.input_tokens,
                    output_tokens=payload.output_tokens,
                    provider=payload.provider,
                    model=payload.model,
                    adapter=payload.adapter,
                    base_url=payload.base_url,
                    instruct=instruct,
                    context=context,
                    reasoning_content=payload.reasoning_content,
                    adapter_request=payload.adapter_request,
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
            self._last_step_index[event.run_id] = max(
                self._last_step_index.get(event.run_id, 0),
                event.step_index,
            )
            return
        if isinstance(event, RunEnd):
            if event.error is not None:
                self._append_runtime_failure_step(event)
            self._store.finish_run(
                run_id=event.run_id,
                status=event.status,
                error=event.error,
                finished_at=event.finished_at,
            )

    def _append_runtime_failure_step(self, event: RunEnd) -> None:
        step_index = self._last_step_index.get(event.run_id, 0) + 1
        self._store.append_step(
            run_id=event.run_id,
            step_index=step_index,
            kind="system",
            status="failed",
            input=(),
            output=(TextPart(text=event.error or "Run failed."),),
            payload=RuntimeStepPayload(),
            error=event.error,
            started_at=event.finished_at,
            finished_at=event.finished_at,
        )
        self._last_step_index[event.run_id] = step_index
