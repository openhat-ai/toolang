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
    message_text,
    parts_from_data,
    parts_to_data,
)
from .events import (
    RunBegin,
    RunEnd,
    RunStarting,
    RunSteering,
    RunStopping,
    RunWaiting,
    StepBegin,
    StepEnd,
    TraceEvent,
)
from .records import (
    EventDomain,
    EventRecord,
    CommandKind,
    CommandApply,
    CommandRecord,
    InputRef,
    OutputRef,
    RunRecord,
    RunStatus,
    StepInputItem,
    StepKind,
    StepPath,
    StepRecord,
    StepStatus,
    ThreadPeer,
    ThreadRecord,
    UpdateKind,
    UpdateRecord,
    input_ref_from_data,
    input_ref_to_data,
    output_ref_from_data,
    output_ref_to_data,
    step_input_items_from_data,
    step_input_items_to_data,
    trace_index,
    trace_parent,
    trace_run,
)
from .stream import trace_event_payload

_SCHEMA_VERSION = 11
_DEFAULT_BINDING = object()


class RunStore:
    """Durable thread and run truth for one agent."""

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

    def accept_run(
        self,
        *,
        run_id: str,
        command_index: int,
        parent: str | None,
        thread: str,
        input: Message,
        context: Mapping[str, Any],
        created_at: str,
    ) -> RunRecord:
        """Project one accepted start command into a pending run."""

        origin = str(context.get("origin") or "chat")
        with self._lock:
            self._ensure_thread_locked(
                thread_id=thread,
                origin=origin,
                peer=None,
                parent=None,
                created_at=created_at,
                updated_at=created_at,
            )
            self._conn.execute(
                """
                INSERT INTO runs(
                    id, parent, thread, input, output, context, status, error,
                    created_at, started_at, finished_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'pending', NULL, ?, NULL, NULL)
                ON CONFLICT(id) DO NOTHING
                """,
                (
                    run_id,
                    parent,
                    thread,
                    _dump_json(input_ref_to_data(InputRef(cmd=command_index))),
                    None,
                    _dump_json(dict(context)),
                    created_at,
                ),
            )
            self._conn.execute(
                """
                INSERT INTO commands(
                    run, "index", kind, apply, input, context, status, error,
                    created_at, finished_at
                ) VALUES (?, ?, 'start', 'now', ?, ?, 'pending', NULL, ?, NULL)
                ON CONFLICT(run, "index") DO NOTHING
                """,
                (
                    run_id,
                    command_index,
                    _dump_json(input.to_data()),
                    _dump_json(dict(context)),
                    created_at,
                ),
            )
            self._conn.execute(
                """
                INSERT INTO command_counters(run, next_index)
                VALUES (?, ?)
                ON CONFLICT(run) DO UPDATE SET
                    next_index = MAX(command_counters.next_index, excluded.next_index)
                """,
                (run_id, command_index + 1),
            )
            run_row = self._conn.execute(
                "SELECT * FROM runs WHERE id = ?", (run_id,)
            ).fetchone()
            command_row = self._conn.execute(
                'SELECT * FROM commands WHERE run = ? AND "index" = ?',
                (run_id, command_index),
            ).fetchone()
            self._conn.commit()
        if run_row is None or command_row is None:
            raise RuntimeError(f"accepted run projection failed: {run_id}")
        run = _run_from_row(run_row)
        command = _command_from_row(command_row)
        if (
            run.parent != parent
            or run.thread != thread
            or run.input != InputRef(cmd=command_index)
            or any(run.context.get(key) != value for key, value in context.items())
            or command.kind != "start"
            or command.input != input
            or command.context != dict(context)
            or command.created_at != created_at
        ):
            raise ValueError(f"conflicting accepted run event: {run_id}")
        return run

    def accept_command(
        self,
        *,
        run_id: str,
        index: int,
        kind: CommandKind,
        apply: CommandApply,
        input: Message | None,
        context: Mapping[str, Any],
        created_at: str,
    ) -> CommandRecord:
        """Project one accepted steer or stop command."""

        with self._lock:
            if (
                self._conn.execute(
                    "SELECT 1 FROM runs WHERE id = ?", (run_id,)
                ).fetchone()
                is None
            ):
                raise ValueError(f"run not found: {run_id}")
            self._conn.execute(
                """
                INSERT INTO commands(
                    run, "index", kind, apply, input, context, status, error,
                    created_at, finished_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'pending', NULL, ?, NULL)
                ON CONFLICT(run, "index") DO NOTHING
                """,
                (
                    run_id,
                    index,
                    kind,
                    apply,
                    _dump_json(input.to_data()) if input is not None else None,
                    _dump_json(dict(context)),
                    created_at,
                ),
            )
            row = self._conn.execute(
                'SELECT * FROM commands WHERE run = ? AND "index" = ?',
                (run_id, index),
            ).fetchone()
            self._conn.commit()
        if row is None:
            raise RuntimeError(f"accepted command projection failed: {run_id}:{index}")
        command = _command_from_row(row)
        if (
            command.kind != kind
            or command.apply != apply
            or command.input != input
            or command.context != dict(context)
            or command.created_at != created_at
        ):
            raise ValueError(f"conflicting accepted command event: {run_id}:{index}")
        return command

    def begin_run(
        self,
        *,
        run_id: str,
        started_at: str,
        context: Mapping[str, Any] | None = None,
    ) -> RunRecord:
        """Project run execution beginning."""

        with self._lock:
            existing = self._conn.execute(
                "SELECT context FROM runs WHERE id = ?", (run_id,)
            ).fetchone()
            if existing is None:
                raise ValueError(f"run not found: {run_id}")
            stored = _load_json(str(existing["context"]))
            merged = {
                **(dict(stored) if isinstance(stored, Mapping) else {}),
                **dict(context or {}),
            }
            self._conn.execute(
                """
                UPDATE runs
                SET status = 'running',
                    context = ?,
                    started_at = COALESCE(started_at, ?)
                WHERE id = ? AND status IN ('pending', 'running')
                """,
                (_dump_json(merged), started_at, run_id),
            )
            row = self._conn.execute(
                "SELECT * FROM runs WHERE id = ?", (run_id,)
            ).fetchone()
            self._conn.commit()
        if row is None:
            raise ValueError(f"run not found: {run_id}")
        run = _run_from_row(row)
        if run.started_at != started_at or any(
            run.context.get(key) != value for key, value in (context or {}).items()
        ):
            raise ValueError(f"conflicting run_begin event: {run_id}")
        return run

    def finish_commands(
        self,
        *,
        run_id: str,
        indexes: Sequence[int],
        finished_at: str,
    ) -> None:
        """Mark pending commands consumed by one execution event as finished."""

        command_indexes = tuple(dict.fromkeys(int(index) for index in indexes))
        if not command_indexes:
            return
        placeholders = ", ".join("?" for _ in command_indexes)
        with self._lock:
            self._conn.execute(
                f"""
                UPDATE commands
                SET status = 'finished', finished_at = ?
                WHERE run = ? AND "index" IN ({placeholders}) AND status = 'pending'
                """,
                (finished_at, run_id, *command_indexes),
            )
            self._conn.commit()

    def cancel_pending_commands(self, *, run_id: str, finished_at: str) -> None:
        """Cancel commands that were accepted but never consumed."""

        with self._lock:
            self._conn.execute(
                """
                UPDATE commands
                SET status = 'canceled', finished_at = ?
                WHERE run = ? AND status = 'pending'
                """,
                (finished_at, run_id),
            )
            self._conn.commit()

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
        output: OutputRef | None = None,
    ) -> RunRecord:
        now = finished_at or utc_now()
        with self._lock:
            self._conn.execute(
                """
                UPDATE runs
                SET status = ?, error = ?, output = ?, finished_at = ?
                WHERE id = ? AND status IN ('pending', 'running')
                """,
                (
                    status,
                    error,
                    _dump_json(output_ref_to_data(output))
                    if output is not None
                    else None,
                    now,
                    run_id,
                ),
            )
            row = self._conn.execute(
                "SELECT * FROM runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            self._conn.commit()
        if row is None:
            raise RuntimeError(f"run not found: {run_id}")
        run = _run_from_row(row)
        if (
            run.status != status
            or run.error != error
            or run.output != output
            or run.finished_at != now
        ):
            raise ValueError(f"conflicting run_end event: {run_id}")
        return run

    def get_run(self, *, run_id: str) -> RunRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM runs WHERE id = ?",
                (run_id,),
            ).fetchone()
        return _run_from_row(row) if row is not None else None

    def run_output(self, *, run_id: str) -> tuple[Part, ...]:
        """Return the parts referenced by one run's durable output edge."""

        run = self.get_run(run_id=run_id)
        if run is None:
            raise ValueError(f"run not found: {run_id}")
        if run.output is None:
            return ()
        parent = trace_parent(run.output.step)
        index = trace_index(run.output.step)
        if parent is None or index is None:
            return ()
        with self._lock:
            row = self._conn.execute(
                'SELECT * FROM steps WHERE parent = ? AND "index" = ?',
                (parent, index),
            ).fetchone()
        if row is None:
            return ()
        output = _step_from_row(row).output
        if run.output.part is None:
            return output
        if 0 <= run.output.part < len(output):
            return (output[run.output.part],)
        return ()

    def run_output_text(self, *, run_id: str) -> str:
        """Return the text projection of one run's durable output."""

        return message_text(self.run_output(run_id=run_id))

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
            clauses.append("thread = ?")
            params.append(thread_id)
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        if not include_superseded:
            clauses.append("json_extract(context, '$.superseded') IS NULL")
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

        clauses = ["thread = ?"]
        params: list[object] = [thread_id]
        if not include_superseded:
            clauses.append("json_extract(context, '$.superseded') IS NULL")
        query = f"""
            SELECT * FROM runs
            WHERE {" AND ".join(clauses)}
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
                "SELECT rowid, * FROM runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            if anchor is None:
                raise ValueError(f"run not found: {run_id}")
            rows = self._conn.execute(
                """
                SELECT * FROM runs
                WHERE thread = ?
                  AND json_extract(context, '$.superseded') IS NULL
                  AND (
                    created_at < ?
                    OR (created_at = ? AND rowid < ?)
                  )
                ORDER BY created_at ASC, rowid ASC
                """,
                (
                    anchor["thread"],
                    anchor["created_at"],
                    anchor["created_at"],
                    anchor["rowid"],
                ),
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

        def remap_path(value: str | None) -> str | None:
            if value is None:
                return None
            for source_id, target_id in run_id_map.items():
                if value == source_id:
                    return target_id
                prefix = f"{source_id}/"
                if value.startswith(prefix):
                    return f"{target_id}/{value[len(prefix) :]}"
            return value

        with self._lock:
            copied: list[sqlite3.Row] = []
            for source_run_id, target_run_id in zip(
                source_run_ids, target_run_ids, strict=True
            ):
                source_run = self._conn.execute(
                    "SELECT * FROM runs WHERE id = ?",
                    (source_run_id,),
                ).fetchone()
                if source_run is None:
                    raise ValueError(f"run not found: {source_run_id}")
                self._conn.execute(
                    """
                    INSERT INTO runs(
                        id,
                        parent,
                        thread,
                        input,
                        output,
                        context,
                        status,
                        error,
                        created_at,
                        started_at,
                        finished_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        target_run_id,
                        remap_path(source_run["parent"]),
                        target_thread_id,
                        source_run["input"],
                        source_run["output"],
                        source_run["context"],
                        source_run["status"],
                        source_run["error"],
                        source_run["created_at"],
                        source_run["started_at"],
                        source_run["finished_at"],
                    ),
                )
                command_rows = self._conn.execute(
                    """
                    SELECT * FROM commands
                    WHERE run = ?
                    ORDER BY "index" ASC
                    """,
                    (source_run_id,),
                ).fetchall()
                self._conn.executemany(
                    """
                    INSERT INTO commands(
                        run,
                        "index",
                        kind,
                        apply,
                        input,
                        context,
                        status,
                        error,
                        created_at,
                        finished_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            target_run_id,
                            row["index"],
                            row["kind"],
                            row["apply"],
                            row["input"],
                            row["context"],
                            row["status"],
                            row["error"],
                            row["created_at"],
                            row["finished_at"],
                        )
                        for row in command_rows
                    ],
                )
                step_rows = self._conn.execute(
                    """
                    SELECT * FROM steps
                    WHERE parent = ? OR parent LIKE ?
                    ORDER BY parent ASC, "index" ASC
                    """,
                    (source_run_id, f"{source_run_id}/%"),
                ).fetchall()
                self._conn.executemany(
                    """
                    INSERT INTO steps(
                        parent,
                        "index",
                        kind,
                        input,
                        output,
                        context,
                        detail,
                        status,
                        error,
                        created_at,
                        started_at,
                        finished_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            remap_path(row["parent"]),
                            row["index"],
                            row["kind"],
                            row["input"],
                            row["output"],
                            row["context"],
                            row["detail"],
                            row["status"],
                            row["error"],
                            row["created_at"],
                            row["started_at"],
                            row["finished_at"],
                        )
                        for row in step_rows
                    ],
                )
                inserted = self._conn.execute(
                    "SELECT * FROM runs WHERE id = ?",
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
                WHERE thread = ?
                  AND json_extract(context, '$.superseded') IS NULL
                  AND created_at >= ?
                ORDER BY created_at
                """,
                (anchor.thread, anchor.created_at),
            ).fetchall()
            self._conn.executemany(
                "UPDATE runs SET context = json_patch(context, ?) WHERE id = ?",
                [
                    (_dump_json({"superseded": dict(superseded)}), str(row["id"]))
                    for row in rows
                ],
            )
            updated = self._conn.execute(
                """
                SELECT * FROM runs
                WHERE thread = ?
                  AND json_extract(context, '$.superseded') IS NOT NULL
                  AND created_at >= ?
                ORDER BY created_at
                """,
                (anchor.thread, anchor.created_at),
            ).fetchall()
            self._conn.commit()
        return tuple(_run_from_row(row) for row in updated)

    def begin_step(
        self,
        *,
        parent: str,
        index: int,
        kind: StepKind,
        input: Sequence[StepInputItem],
        context: Mapping[str, Any],
        started_at: str,
    ) -> StepRecord:
        """Project one step_begin event."""

        with self._lock:
            self._conn.execute(
                """
                INSERT INTO steps(
                    parent, "index", kind, input, output, context, detail,
                    status, error, created_at, started_at, finished_at
                ) VALUES (?, ?, ?, ?, '[]', ?, '{}', 'running', NULL, ?, ?, NULL)
                ON CONFLICT(parent, "index") DO NOTHING
                """,
                (
                    parent,
                    index,
                    kind,
                    _dump_json(step_input_items_to_data(tuple(input))),
                    _dump_json(dict(context)),
                    started_at,
                    started_at,
                ),
            )
            row = self._conn.execute(
                'SELECT * FROM steps WHERE parent = ? AND "index" = ?',
                (parent, index),
            ).fetchone()
            self._conn.commit()
        if row is None:
            raise RuntimeError(f"step begin projection failed: {parent}/{index}")
        step = _step_from_row(row)
        if (
            step.kind != kind
            or step.input != tuple(input)
            or step.context != dict(context)
            or step.started_at != started_at
        ):
            raise ValueError(f"conflicting step_begin event: {parent}/{index}")
        return step

    def finish_step(
        self,
        *,
        parent: str,
        index: int,
        kind: StepKind,
        status: StepStatus,
        output: Sequence[Part],
        detail: Mapping[str, Any],
        error: str | None,
        finished_at: str,
    ) -> StepRecord:
        """Project one step_end event."""

        with self._lock:
            existing = self._conn.execute(
                'SELECT * FROM steps WHERE parent = ? AND "index" = ?',
                (parent, index),
            ).fetchone()
            if existing is None:
                raise ValueError(f"step not found: {parent}/{index}")
            existing_step = _step_from_row(existing)
            if existing_step.kind != kind:
                raise ValueError(f"step kind changed: {parent}/{index}")
            if existing_step.status == "running":
                self._conn.execute(
                    """
                    UPDATE steps
                    SET output = ?, detail = ?, status = ?, error = ?, finished_at = ?
                    WHERE parent = ? AND "index" = ?
                    """,
                    (
                        _dump_json(parts_to_data(output)),
                        _dump_json(dict(detail)),
                        status,
                        error,
                        finished_at,
                        parent,
                        index,
                    ),
                )
            row = self._conn.execute(
                'SELECT * FROM steps WHERE parent = ? AND "index" = ?',
                (parent, index),
            ).fetchone()
            self._conn.commit()
        if row is None:
            raise RuntimeError(f"step end projection failed: {parent}/{index}")
        step = _step_from_row(row)
        if (
            step.status != status
            or step.output != tuple(output)
            or step.detail != dict(detail)
            or step.error != error
            or step.finished_at != finished_at
        ):
            raise ValueError(f"conflicting step_end event: {parent}/{index}")
        return step

    def list_steps(self, *, run_id: str) -> list[StepRecord]:
        with self._lock:
            rows = self._conn.execute(
                'SELECT * FROM steps WHERE parent = ? OR parent LIKE ? ORDER BY parent ASC, "index" ASC',
                (run_id, f"{run_id}/%"),
            ).fetchall()
        return [_step_from_row(row) for row in rows]

    def list_steps_for_runs(
        self, *, run_ids: Sequence[str]
    ) -> dict[str, list[StepRecord]]:
        run_id_list = [item for item in run_ids if item]
        if not run_id_list:
            return {}
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT * FROM steps
                WHERE {" OR ".join("(parent = ? OR parent LIKE ?)" for _ in run_id_list)}
                ORDER BY parent ASC, "index" ASC
                """,
                tuple(
                    item for run_id in run_id_list for item in (run_id, f"{run_id}/%")
                ),
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

    def recent_conversation_messages(
        self,
        *,
        thread_id: str,
        limit: int = 20,
        exclude_run_id: str | None = None,
    ) -> list[Message]:
        runs = self._conversation_runs(
            thread_id=thread_id,
            limit=max(limit + (1 if exclude_run_id else 0), 20),
        )
        if exclude_run_id is not None:
            runs = [run for run in runs if run.run_id != exclude_run_id]
            runs = runs[-limit:]
        steps_by_run = self.list_steps_for_runs(
            run_ids=tuple(run.run_id for run in runs)
        )
        results: list[Message] = []
        for run in runs:
            inputs = self.list_commands(run_id=run.run_id)
            if inputs:
                results.extend(
                    item.input for item in inputs if item.input is not None
                )
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
        steps_by_run = self.list_steps_for_runs(
            run_ids=tuple(run.run_id for run in runs)
        )
        results: list[Message] = []
        for run in runs:
            inputs = self.list_commands(run_id=run.run_id)
            input_messages = [
                item.input for item in inputs if item.input is not None
            ]
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
        current = list(
            self.list_thread_runs_chronological(thread_id=thread_id, limit=None)
        )
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
            try:
                self._conn.execute("BEGIN IMMEDIATE")
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
            except Exception:
                self._conn.rollback()
                raise
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
                WHERE {" AND ".join(clauses)}
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

    def reserve_command_index(self, *, run_id: str) -> int:
        """Atomically reserve the next command index for one run."""

        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                if (
                    self._conn.execute(
                        "SELECT 1 FROM runs WHERE id = ?", (run_id,)
                    ).fetchone()
                    is None
                ):
                    raise ValueError(f"run not found: {run_id}")
                self._conn.execute(
                    """
                    INSERT INTO command_counters(run, next_index)
                    SELECT ?, COALESCE(MAX("index"), -1) + 1
                    FROM commands
                    WHERE run = ?
                    ON CONFLICT(run) DO NOTHING
                    """,
                    (run_id, run_id),
                )
                row = self._conn.execute(
                    """
                    UPDATE command_counters
                    SET next_index = next_index + 1
                    WHERE run = ?
                    RETURNING next_index - 1 AS reserved_index
                    """,
                    (run_id,),
                ).fetchone()
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        if row is None:
            raise RuntimeError(f"command index reservation failed: {run_id}")
        return int(row["reserved_index"])

    def get_command(self, *, run_id: str, index: int) -> CommandRecord | None:
        with self._lock:
            row = self._conn.execute(
                'SELECT * FROM commands WHERE run = ? AND "index" = ?',
                (run_id, index),
            ).fetchone()
        return _command_from_row(row) if row is not None else None

    def list_commands(
        self,
        *,
        run_id: str,
        kind: CommandKind | None = None,
    ) -> tuple[CommandRecord, ...]:
        clauses = ["run = ?"]
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
        """Return accepted commands not yet consumed by an execution event."""

        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM commands
                WHERE run = ? AND kind = ? AND status = 'pending'
                ORDER BY "index" ASC
                """,
                (run_id, kind),
            ).fetchall()
        return tuple(_command_from_row(row) for row in rows)

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL;")
            self._conn.execute("PRAGMA synchronous=NORMAL;")
            version = int(self._conn.execute("PRAGMA user_version").fetchone()[0])
            if version != _SCHEMA_VERSION:
                self._conn.execute("DROP TABLE IF EXISTS steps")
                self._conn.execute("DROP TABLE IF EXISTS command_counters")
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
                    id TEXT PRIMARY KEY,
                    parent TEXT,
                    thread TEXT NOT NULL,
                    input TEXT NOT NULL,
                    output TEXT,
                    context TEXT NOT NULL,
                    status TEXT NOT NULL,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS commands (
                    run TEXT NOT NULL,
                    "index" INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    apply TEXT NOT NULL,
                    input TEXT,
                    context TEXT NOT NULL,
                    status TEXT NOT NULL,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    finished_at TEXT,
                    PRIMARY KEY(run, "index"),
                    FOREIGN KEY(run) REFERENCES runs(id)
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS command_counters (
                    run TEXT PRIMARY KEY,
                    next_index INTEGER NOT NULL,
                    FOREIGN KEY(run) REFERENCES runs(id) ON DELETE CASCADE
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS steps (
                    parent TEXT NOT NULL,
                    "index" INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    input TEXT NOT NULL,
                    output TEXT NOT NULL,
                    context TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    status TEXT NOT NULL,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    PRIMARY KEY(parent, "index")
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
                "CREATE INDEX IF NOT EXISTS idx_runs_thread_created ON runs(thread, created_at)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_threads_updated ON threads(updated_at)"
            )
            self._conn.execute(
                'CREATE INDEX IF NOT EXISTS idx_steps_parent_index ON steps(parent, "index")'
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_updates_created ON updates(created_at)"
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
                    raise RuntimeError(
                        f"thread not found after parent update: {thread_id}"
                    )
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


def run_store_path(toolang_root: Path, agent_name: str) -> Path:
    return toolang_root / "agents" / agent_name / ".runtime" / "runs.db"


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _dump_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _load_json(value: str) -> Any:
    return json.loads(value)


def _run_from_row(row: sqlite3.Row) -> RunRecord:
    context_raw = _load_json(str(row["context"])) if row["context"] is not None else {}
    output_raw = _load_json(str(row["output"])) if row["output"] is not None else None
    return RunRecord(
        id=str(row["id"]),
        parent=str(row["parent"]) if row["parent"] is not None else None,
        thread=str(row["thread"]),
        input=input_ref_from_data(
            cast(Mapping[str, Any], _load_json(str(row["input"])))
        ),
        output=output_ref_from_data(
            cast(Mapping[str, Any], output_raw)
            if isinstance(output_raw, Mapping)
            else None
        ),
        context=dict(context_raw) if isinstance(context_raw, Mapping) else {},
        status=cast(RunStatus, row["status"]),
        error=str(row["error"]) if row["error"] is not None else None,
        created_at=str(row["created_at"]),
        started_at=str(row["started_at"] or ""),
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
    context_raw = _load_json(str(raw["context"]))
    detail_raw = _load_json(str(raw["detail"]))
    input_items = (
        input_raw
        if isinstance(input_raw, Sequence)
        and not isinstance(input_raw, (str, bytes, bytearray))
        else []
    )
    output_items = (
        output_raw
        if isinstance(output_raw, Sequence)
        and not isinstance(output_raw, (str, bytes, bytearray))
        else []
    )
    return StepRecord(
        parent=str(raw["parent"]),
        index=int(cast(int | str, raw["index"])),
        kind=cast(StepKind, raw["kind"]),
        input=step_input_items_from_data(
            [item for item in input_items if isinstance(item, Mapping)]
        ),
        output=parts_from_data(
            [item for item in output_items if isinstance(item, Mapping)]
        ),
        context=dict(context_raw) if isinstance(context_raw, Mapping) else {},
        detail=dict(detail_raw) if isinstance(detail_raw, Mapping) else {},
        status=cast(StepStatus, raw["status"]),
        error=str(raw["error"]) if raw["error"] is not None else None,
        created_at=str(raw["created_at"]),
        started_at=str(raw["started_at"]),
        finished_at=str(raw["finished_at"]) if raw["finished_at"] is not None else None,
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
    input_raw = _load_json(str(row["input"])) if row["input"] is not None else None
    context_raw = _load_json(str(row["context"])) if row["context"] is not None else {}
    return CommandRecord(
        run=str(row["run"]),
        index=int(row["index"]),
        kind=cast(CommandKind, row["kind"]),
        apply=cast(CommandApply, row["apply"]),
        input=Message.from_data(input_raw) if isinstance(input_raw, Mapping) else None,
        context=dict(context_raw) if isinstance(context_raw, Mapping) else {},
        status=row["status"],
        error=str(row["error"]) if row["error"] is not None else None,
        created_at=str(row["created_at"]),
        finished_at=str(row["finished_at"]) if row["finished_at"] is not None else None,
    )


def _replay_messages_from_step(step: StepRecord) -> list[Message]:
    role = _role_for_step(step.kind)
    if role is None or not step.output:
        return []
    meta: dict[str, Any] = {}
    if step.error is not None:
        meta["error"] = step.error
    reasoning_content = step.detail.get("reasoning_content")
    if isinstance(reasoning_content, str) and reasoning_content:
        meta["reasoning_content"] = reasoning_content
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


def _recent_valid_model_history(
    messages: Sequence[Message], *, limit: int
) -> list[Message]:
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


def _valid_model_history_groups(
    messages: Sequence[Message],
) -> list[tuple[Message, ...]]:
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
    return tuple(
        part.tool_call_id
        for part in message.parts
        if isinstance(part, ToolCallPart) and part.tool_call_id
    )


def _message_tool_result_ids(message: Message) -> set[str]:
    return {
        part.tool_call_id
        for part in message.parts
        if isinstance(part, ToolResultPart) and part.tool_call_id
    }


class PersistSink:
    """Persist trace events into the execution store."""

    def __init__(self, store: RunStore, *, agent_id: str | None = None) -> None:
        self._store = store
        self._agent_id = agent_id
        self._lock = threading.Lock()
        self._last_step_index: dict[str, int] = {}
        self._failed_runs: set[str] = set()
        self._locals: dict[StepPath, dict[str, InputRef | OutputRef]] = {}
        self._bindings: dict[StepPath, str | None] = {}

    def on_event(self, event: TraceEvent) -> None:
        """Project one trace event atomically."""

        with self._lock:
            self._on_event(event)
            self._append_event(event)

    def _on_event(self, event: TraceEvent) -> None:
        if event.type in {"run_waiting", "run_starting"}:
            accepted = cast(RunWaiting | RunStarting, event)
            self._store.accept_run(
                run_id=accepted.run,
                command_index=accepted.cmd,
                parent=accepted.parent,
                thread=accepted.thread,
                input=accepted.input,
                context=accepted.context,
                created_at=accepted.created_at,
            )
            return
        if event.type == "run_steering":
            steering = cast(RunSteering, event)
            self._store.accept_command(
                run_id=steering.run,
                index=steering.cmd,
                kind="steer",
                apply=steering.apply,
                input=steering.input,
                context=steering.context,
                created_at=steering.created_at,
            )
            return
        if event.type == "run_stopping":
            stopping = cast(RunStopping, event)
            self._store.accept_command(
                run_id=stopping.run,
                index=stopping.cmd,
                kind="stop",
                apply=stopping.apply,
                input=stopping.input,
                context=stopping.context,
                created_at=stopping.created_at,
            )
            return
        if event.type == "run_begin":
            run_begin = cast(RunBegin, event)
            self._store.finish_commands(
                run_id=run_begin.run,
                indexes=(run_begin.input.cmd,),
                finished_at=run_begin.started_at,
            )
            self._store.begin_run(
                run_id=run_begin.run,
                context=run_begin.context,
                started_at=run_begin.started_at,
            )
            self._locals[run_begin.run] = {"_": run_begin.input}
            return
        if event.type == "step_begin":
            step_begin = cast(StepBegin, event)
            run_id = trace_run(step_begin.step)
            self._store.finish_commands(
                run_id=run_id,
                indexes=tuple(
                    item.cmd for item in step_begin.input if isinstance(item, InputRef)
                ),
                finished_at=step_begin.started_at,
            )
            parent = trace_parent(step_begin.step)
            index = trace_index(step_begin.step)
            if parent is None or index is None:
                raise ValueError(f"step_begin requires a step path: {step_begin.step}")
            locals = self._locals.setdefault(parent, {})
            explicit = tuple(step_begin.input)
            steer_inputs = [
                item for item in explicit if isinstance(item, InputRef) and item.cmd > 0
            ]
            if steer_inputs:
                locals["_"] = steer_inputs[-1]
            reads = step_begin.context.get("reads")
            inferred = (
                tuple(locals[name] for name in reads if isinstance(name, str) and name in locals)
                if isinstance(reads, Sequence) and not isinstance(reads, (str, bytes))
                else ()
            )
            inputs = _unique_step_inputs((*explicit, *inferred))
            self._store.begin_step(
                parent=parent,
                index=index,
                kind=step_begin.kind,
                input=inputs,
                context=step_begin.context,
                started_at=step_begin.started_at,
            )
            self._locals[step_begin.step] = dict(locals)
            raw_binding = step_begin.context.get("binding", _DEFAULT_BINDING)
            self._bindings[step_begin.step] = (
                "_"
                if raw_binding is _DEFAULT_BINDING and step_begin.kind == "model"
                else raw_binding
                if isinstance(raw_binding, str)
                else None
            )
            if parent == run_id:
                self._last_step_index[run_id] = max(
                    self._last_step_index.get(run_id, -1), index
                )
            return
        if event.type == "step_end":
            step_end = cast(StepEnd, event)
            if step_end.status == "failed":
                self._failed_runs.add(trace_run(step_end.step))
            parent = trace_parent(step_end.step)
            index = trace_index(step_end.step)
            if parent is None or index is None:
                raise ValueError(f"step_end requires a step path: {step_end.step}")
            self._store.finish_step(
                parent=parent,
                index=index,
                kind=step_end.kind,
                status=step_end.status,
                output=step_end.output,
                detail=step_end.detail,
                error=step_end.error,
                finished_at=step_end.finished_at,
            )
            binding = self._bindings.pop(step_end.step, None)
            if step_end.status == "finished" and binding is not None:
                self._locals.setdefault(parent, {})[binding] = OutputRef(
                    step=step_end.step
                )
            self._locals.pop(step_end.step, None)
            return
        if event.type == "run_end":
            run_end = cast(RunEnd, event)
            if run_end.input is not None:
                self._store.finish_commands(
                    run_id=run_end.run,
                    indexes=(run_end.input.cmd,),
                    finished_at=run_end.finished_at,
                )
            if run_end.status == "failed" and run_end.run not in self._failed_runs:
                self._append_runtime_failure_step(run_end)
            projected_output = self._locals.get(run_end.run, {}).get("_")
            self._store.finish_run(
                run_id=run_end.run,
                status=run_end.status,
                error=run_end.error,
                output=(
                    projected_output
                    if isinstance(projected_output, OutputRef)
                    else run_end.output
                ),
                finished_at=run_end.finished_at,
            )
            self._store.cancel_pending_commands(
                run_id=run_end.run, finished_at=run_end.finished_at
            )
            self._locals.pop(run_end.run, None)

    def _append_event(self, event: TraceEvent) -> None:
        payload = trace_event_payload(event)
        run_id = payload.get("run")
        if not isinstance(run_id, str):
            step = payload.get("step")
            if isinstance(step, str) and step:
                run_id = trace_run(step)
        thread_id = payload.get("thread")
        context = payload.get("context")
        if not isinstance(thread_id, str) and isinstance(context, dict):
            thread_id = context.get("thread")
        if not isinstance(thread_id, str) and isinstance(run_id, str) and run_id:
            run = self._store.get_run(run_id=run_id)
            if run is not None:
                thread_id = run.thread
        if isinstance(run_id, str) and run_id:
            self._store.append_event(
                domain="run",
                domain_id=run_id,
                type=event.type,
                payload=payload,
            )
        if self._agent_id and isinstance(event, (RunBegin, RunEnd)):
            agent_payload = dict(payload)
            if isinstance(event, RunBegin):
                agent_payload["status"] = "running"
            self._store.append_event(
                domain="agent",
                domain_id=self._agent_id,
                type="thread_update",
                payload=agent_payload,
            )
        if isinstance(thread_id, str) and thread_id:
            self._store.append_event(
                domain="thread",
                domain_id=thread_id,
                type=event.type,
                payload=payload,
            )

    def _append_runtime_failure_step(self, event: RunEnd) -> None:
        step_index = self._last_step_index.get(event.run, -1) + 1
        self._store.begin_step(
            parent=event.run,
            index=step_index,
            kind="system",
            input=(),
            context={},
            started_at=event.finished_at,
        )
        self._store.finish_step(
            parent=event.run,
            index=step_index,
            kind="system",
            status="failed",
            output=(TextPart(text=event.error or "Run failed."),),
            detail={},
            error=event.error,
            finished_at=event.finished_at,
        )
        self._last_step_index[event.run] = step_index


def _unique_step_inputs(items: Sequence[StepInputItem]) -> tuple[StepInputItem, ...]:
    result: list[StepInputItem] = []
    for item in items:
        if item not in result:
            result.append(item)
    return tuple(result)
