"""SQLite-backed durable execution truth."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
from typing import Any, cast

from toolang.base.types.message import (
    Message,
    Part,
    ToolCallPart,
    ToolResultPart,
    message_text,
    parts_from_data,
    parts_to_data,
)
from toolang.common.time import utc_now
from .records import (
    RunControlRecord,
    RunControlRef,
    OutputRef,
    RunRecord,
    StepInputItem,
    StepRecord,
    ThreadPeer,
    ThreadControlRecord,
    ThreadControlRef,
    ThreadRecord,
    UpdateRecord,
    step_message_role,
    step_input_items_from_data,
    step_input_items_to_data,
    trace_index,
    trace_parent,
)
from .types import (
    RunControlKind,
    RunControlTiming,
    RunStatus,
    StepKind,
    StepStatus,
)

_SCHEMA_VERSION = 12


class RunStore:
    """Durable thread and run truth for one agent."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            db_path.as_posix(),
            check_same_thread=False,
            timeout=30,
        )
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._init_schema()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def accept_start(
        self,
        *,
        run_id: str,
        parent: str | None,
        thread: str,
        input: Message,
        context: Mapping[str, Any],
        request_id: str | None,
        created_at: str,
    ) -> tuple[RunRecord, RunControlRecord, bool]:
        """Atomically accept one run and return whether this caller owns it."""

        if not run_id or "/" in run_id:
            raise ValueError(f"invalid run id: {run_id!r}")

        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                existing_run = self._conn.execute(
                    "SELECT * FROM runs WHERE id = ?", (run_id,)
                ).fetchone()
                if existing_run is not None:
                    existing_control = self._conn.execute(
                        'SELECT * FROM run_controls WHERE run = ? AND "index" = 0',
                        (run_id,),
                    ).fetchone()
                    if existing_control is None:
                        raise ValueError(f"run start control is missing: {run_id}")
                    control = _run_control_from_row(existing_control)
                    if request_id is None or control.request_id != request_id:
                        raise ValueError(f"run already exists: {run_id}")
                    existing = _run_from_row(existing_run)
                    if (
                        existing.parent != parent
                        or existing.thread != thread
                        or control.input != input
                        or control.context != dict(context)
                    ):
                        raise ValueError(f"conflicting run start request: {run_id}")
                    self._conn.commit()
                    return existing, control, False
                if (
                    self._conn.execute(
                        "SELECT 1 FROM threads WHERE thread_id = ?", (thread,)
                    ).fetchone()
                    is None
                ):
                    raise ValueError(f"thread not found: {thread}")
                self._conn.execute(
                    """
                    INSERT INTO runs(
                        id, parent, thread, input, output, context, status, error,
                        superseded_by_thread, superseded_by_index,
                        created_at, started_at, finished_at
                    ) VALUES (?, ?, ?, ?, NULL, ?, 'pending', NULL, NULL, NULL, ?, NULL, NULL)
                    """,
                    (
                        run_id,
                        parent,
                        thread,
                        _dump_json(RunControlRef(index=0).to_data()),
                        _dump_json(dict(context)),
                        created_at,
                    ),
                )
                self._conn.execute(
                    """
                    INSERT INTO run_controls(
                        run, "index", kind, timing, input, request_id, context,
                        status, error, created_at, finished_at
                    ) VALUES (?, 0, 'start', 'immediate', ?, ?, ?, 'pending', NULL, ?, NULL)
                    """,
                    (
                        run_id,
                        _dump_json(input.to_data()),
                        request_id,
                        _dump_json(dict(context)),
                        created_at,
                    ),
                )
                run_row = self._conn.execute(
                    "SELECT * FROM runs WHERE id = ?", (run_id,)
                ).fetchone()
                control_row = self._conn.execute(
                    'SELECT * FROM run_controls WHERE run = ? AND "index" = 0',
                    (run_id,),
                ).fetchone()
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        if run_row is None or control_row is None:
            raise RuntimeError(f"run acceptance failed: {run_id}")
        return _run_from_row(run_row), _run_control_from_row(control_row), True

    def accept_run_control(
        self,
        *,
        run_id: str,
        kind: RunControlKind,
        timing: RunControlTiming,
        input: Message | None,
        context: Mapping[str, Any],
        request_id: str | None,
        created_at: str,
    ) -> tuple[RunControlRecord, bool]:
        """Atomically allocate and accept one steer or stop control."""

        if kind not in {"steer", "stop"}:
            raise ValueError(f"unsupported run control kind: {kind}")
        if timing not in {"immediate", "next_step", "next_call"}:
            raise ValueError(f"unsupported run control timing: {timing}")
        if kind == "steer" and input is None:
            raise ValueError("steer control requires input")

        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                run = self._conn.execute(
                    "SELECT status FROM runs WHERE id = ?", (run_id,)
                ).fetchone()
                if run is None:
                    raise ValueError(f"run not found: {run_id}")
                if request_id is not None:
                    existing = self._conn.execute(
                        "SELECT * FROM run_controls WHERE run = ? AND request_id = ?",
                        (run_id, request_id),
                    ).fetchone()
                    if existing is not None:
                        control = _run_control_from_row(existing)
                        if (
                            control.kind != kind
                            or control.timing != timing
                            or control.input != input
                            or control.context != dict(context)
                        ):
                            raise ValueError(
                                f"conflicting run control request: {run_id}:{request_id}"
                            )
                        self._conn.commit()
                        return control, False
                if str(run["status"]) not in {"pending", "running"}:
                    raise ValueError(f"run is not active: {run_id}")
                row = self._conn.execute(
                    'SELECT COALESCE(MAX("index"), -1) + 1 AS next_index '
                    "FROM run_controls WHERE run = ?",
                    (run_id,),
                ).fetchone()
                index = int(row["next_index"]) if row is not None else 0
                self._conn.execute(
                    """
                    INSERT INTO run_controls(
                        run, "index", kind, timing, input, request_id, context,
                        status, error, created_at, finished_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', NULL, ?, NULL)
                    """,
                    (
                        run_id,
                        index,
                        kind,
                        timing,
                        _dump_json(input.to_data()) if input is not None else None,
                        request_id,
                        _dump_json(dict(context)),
                        created_at,
                    ),
                )
                inserted = self._conn.execute(
                    'SELECT * FROM run_controls WHERE run = ? AND "index" = ?',
                    (run_id, index),
                ).fetchone()
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        if inserted is None:
            raise RuntimeError(f"run control acceptance failed: {run_id}")
        return _run_control_from_row(inserted), True

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

    def finish_run_controls(
        self,
        *,
        run_id: str,
        indexes: Sequence[int],
        finished_at: str,
    ) -> None:
        """Mark pending run_controls consumed by one execution event as finished."""

        control_indexes = tuple(dict.fromkeys(int(index) for index in indexes))
        if not control_indexes:
            return
        placeholders = ", ".join("?" for _ in control_indexes)
        with self._lock:
            self._conn.execute(
                f"""
                UPDATE run_controls
                SET status = 'finished', finished_at = ?
                WHERE run = ? AND "index" IN ({placeholders}) AND status = 'pending'
                """,
                (finished_at, run_id, *control_indexes),
            )
            self._conn.commit()

    def cancel_run_control(
        self, *, run_id: str, index: int, finished_at: str
    ) -> RunControlRecord:
        """Cancel one pending control explicitly withdrawn by its caller."""

        with self._lock:
            self._conn.execute(
                """
                UPDATE run_controls
                SET status = 'canceled', finished_at = ?
                WHERE run = ? AND "index" = ? AND status = 'pending'
                """,
                (finished_at, run_id, index),
            )
            row = self._conn.execute(
                'SELECT * FROM run_controls WHERE run = ? AND "index" = ?',
                (run_id, index),
            ).fetchone()
            self._conn.commit()
        if row is None:
            raise ValueError(f"run control not found: {run_id}:{index}")
        return _run_control_from_row(row)

    def fail_pending_run_controls(
        self, *, run_id: str, finished_at: str, error: str
    ) -> None:
        """Fail controls that can no longer be applied to a terminal run."""

        with self._lock:
            self._conn.execute(
                """
                UPDATE run_controls
                SET status = 'failed', error = ?, finished_at = ?
                WHERE run = ? AND status = 'pending'
                """,
                (error, finished_at, run_id),
            )
            self._conn.commit()

    def create_thread(
        self,
        *,
        thread_id: str,
        origin: str = "chat",
        peer: ThreadPeer | None = None,
        request_id: str | None = None,
        context: Mapping[str, Any] | None = None,
        created_at: str | None = None,
    ) -> tuple[ThreadRecord, ThreadControlRecord, bool]:
        """Atomically create one thread and its create control."""

        now = created_at or utc_now()
        effective_peer = peer or ThreadPeer()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                if request_id is not None:
                    replay = self._conn.execute(
                        "SELECT * FROM thread_controls WHERE request_id = ?",
                        (request_id,),
                    ).fetchone()
                    if replay is not None:
                        control = _thread_control_from_row(replay)
                        if control.kind != "create":
                            raise ValueError(
                                f"conflicting thread control request: {request_id}"
                            )
                        existing = self._conn.execute(
                            "SELECT * FROM threads WHERE thread_id = ?",
                            (control.thread,),
                        ).fetchone()
                        if existing is None:
                            raise RuntimeError(
                                f"thread control result is missing: {control.thread}"
                            )
                        thread = _thread_from_row(existing)
                        if (
                            thread.origin != origin
                            or thread.peer != effective_peer
                            or control.context != dict(context or {})
                        ):
                            raise ValueError(
                                f"conflicting thread control request: {request_id}"
                            )
                        self._conn.commit()
                        return thread, control, False
                existing_thread = self._conn.execute(
                    "SELECT * FROM threads WHERE thread_id = ?", (thread_id,)
                ).fetchone()
                if existing_thread is not None:
                    existing_control = self._conn.execute(
                        'SELECT * FROM thread_controls WHERE thread = ? AND "index" = 0',
                        (thread_id,),
                    ).fetchone()
                    if existing_control is None:
                        raise ValueError(
                            f"thread create control is missing: {thread_id}"
                        )
                    control = _thread_control_from_row(existing_control)
                    thread = _thread_from_row(existing_thread)
                    if (
                        control.kind != "create"
                        or control.context != dict(context or {})
                        or thread.origin != origin
                        or thread.peer != effective_peer
                    ):
                        raise ValueError(f"conflicting thread create: {thread_id}")
                    self._conn.commit()
                    return thread, control, False
                self._conn.execute(
                    """
                    INSERT INTO thread_controls(
                        thread, "index", kind, source_thread, anchor_run, result_run,
                        message, request_id, expected_head_thread, expected_head_index,
                        context, status, error, created_at, finished_at
                    ) VALUES (?, 0, 'create', NULL, NULL, NULL, NULL, ?, NULL, NULL,
                              ?, 'finished', NULL, ?, ?)
                    """,
                    (thread_id, request_id, _dump_json(dict(context or {})), now, now),
                )
                self._conn.execute(
                    """
                    INSERT INTO threads(
                        thread_id, origin, peer, created_by_index, head_index,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, 0, 0, ?, ?)
                    """,
                    (thread_id, origin, _dump_json(effective_peer.to_data()), now, now),
                )
                thread_row = self._conn.execute(
                    "SELECT * FROM threads WHERE thread_id = ?", (thread_id,)
                ).fetchone()
                control_row = self._conn.execute(
                    'SELECT * FROM thread_controls WHERE thread = ? AND "index" = 0',
                    (thread_id,),
                ).fetchone()
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        if thread_row is None or control_row is None:
            raise RuntimeError(f"thread creation failed: {thread_id}")
        return _thread_from_row(thread_row), _thread_control_from_row(control_row), True

    def fork_thread(
        self,
        *,
        thread_id: str,
        source_thread: str,
        anchor_run: str,
        origin: str,
        peer: ThreadPeer,
        result_run: str | None,
        message: Message | None,
        request_id: str | None,
        context: Mapping[str, Any],
        created_at: str,
    ) -> tuple[ThreadRecord, ThreadControlRecord, bool]:
        """Atomically fork one thread without copying execution records."""

        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                if request_id is not None:
                    replay = self._conn.execute(
                        "SELECT * FROM thread_controls WHERE request_id = ?",
                        (request_id,),
                    ).fetchone()
                    if replay is not None:
                        control = _thread_control_from_row(replay)
                        if (
                            control.kind != "fork"
                            or control.source_thread != source_thread
                            or control.anchor_run != anchor_run
                            or control.message != message
                            or control.context != dict(context)
                        ):
                            raise ValueError(
                                f"conflicting thread control request: {request_id}"
                            )
                        existing = self._conn.execute(
                            "SELECT * FROM threads WHERE thread_id = ?",
                            (control.thread,),
                        ).fetchone()
                        if existing is None:
                            raise RuntimeError(
                                f"thread control result is missing: {control.thread}"
                            )
                        self._conn.commit()
                        return _thread_from_row(existing), control, False
                anchor = self._conn.execute(
                    "SELECT thread FROM runs WHERE id = ?", (anchor_run,)
                ).fetchone()
                if anchor is None or str(anchor["thread"]) != source_thread:
                    raise ValueError(f"invalid fork anchor: {anchor_run}")
                if (
                    self._conn.execute(
                        "SELECT 1 FROM threads WHERE thread_id = ?", (thread_id,)
                    ).fetchone()
                    is not None
                ):
                    existing = self._conn.execute(
                        'SELECT * FROM thread_controls WHERE thread = ? AND "index" = 0',
                        (thread_id,),
                    ).fetchone()
                    if existing is None:
                        raise ValueError(f"thread already exists: {thread_id}")
                    control = _thread_control_from_row(existing)
                    if request_id is None or control.request_id != request_id:
                        raise ValueError(f"thread already exists: {thread_id}")
                    row = self._conn.execute(
                        "SELECT * FROM threads WHERE thread_id = ?", (thread_id,)
                    ).fetchone()
                    self._conn.commit()
                    if row is None:
                        raise RuntimeError(f"thread not found: {thread_id}")
                    return _thread_from_row(row), control, False
                self._conn.execute(
                    """
                    INSERT INTO thread_controls(
                        thread, "index", kind, source_thread, anchor_run, result_run,
                        message, request_id, expected_head_thread, expected_head_index,
                        context, status, error, created_at, finished_at
                    ) VALUES (?, 0, 'fork', ?, ?, ?, ?, ?, NULL, NULL, ?,
                              'finished', NULL, ?, ?)
                    """,
                    (
                        thread_id,
                        source_thread,
                        anchor_run,
                        result_run,
                        _dump_json(message.to_data()) if message is not None else None,
                        request_id,
                        _dump_json(dict(context)),
                        created_at,
                        created_at,
                    ),
                )
                self._conn.execute(
                    """
                    INSERT INTO threads(
                        thread_id, origin, peer, created_by_index, head_index,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, 0, 0, ?, ?)
                    """,
                    (
                        thread_id,
                        origin,
                        _dump_json(peer.to_data()),
                        created_at,
                        created_at,
                    ),
                )
                thread_row = self._conn.execute(
                    "SELECT * FROM threads WHERE thread_id = ?", (thread_id,)
                ).fetchone()
                control_row = self._conn.execute(
                    'SELECT * FROM thread_controls WHERE thread = ? AND "index" = 0',
                    (thread_id,),
                ).fetchone()
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        if thread_row is None or control_row is None:
            raise RuntimeError(f"thread fork failed: {thread_id}")
        return _thread_from_row(thread_row), _thread_control_from_row(control_row), True

    def rewind_thread(
        self,
        *,
        thread_id: str,
        anchor_run: str,
        result_run: str | None,
        message: Message | None,
        request_id: str | None,
        expected_head: ThreadControlRef,
        context: Mapping[str, Any],
        created_at: str,
    ) -> tuple[ThreadRecord, ThreadControlRecord, tuple[str, ...], bool]:
        """Atomically rewind one thread using optimistic head comparison."""

        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                if request_id is not None:
                    existing = self._conn.execute(
                        "SELECT * FROM thread_controls WHERE request_id = ?",
                        (request_id,),
                    ).fetchone()
                    if existing is not None:
                        control = _thread_control_from_row(existing)
                        if (
                            control.kind != "rewind"
                            or control.thread != thread_id
                            or control.anchor_run != anchor_run
                            or control.message != message
                            or control.context != dict(context)
                        ):
                            raise ValueError(
                                f"conflicting thread control request: {request_id}"
                            )
                        thread_row = self._conn.execute(
                            "SELECT * FROM threads WHERE thread_id = ?", (thread_id,)
                        ).fetchone()
                        self._conn.commit()
                        if thread_row is None:
                            raise RuntimeError(f"thread not found: {thread_id}")
                        return _thread_from_row(thread_row), control, (), False
                thread_row = self._conn.execute(
                    "SELECT * FROM threads WHERE thread_id = ?", (thread_id,)
                ).fetchone()
                if thread_row is None:
                    raise ValueError(f"thread not found: {thread_id}")
                thread = _thread_from_row(thread_row)
                if thread.head != expected_head:
                    raise ValueError(f"thread head changed: {thread_id}")
                anchor = self._conn.execute(
                    "SELECT rowid FROM runs WHERE id = ? AND thread = ?",
                    (anchor_run, thread_id),
                ).fetchone()
                if anchor is None:
                    raise ValueError(f"invalid rewind anchor: {anchor_run}")
                index_row = self._conn.execute(
                    'SELECT COALESCE(MAX("index"), -1) + 1 AS next_index '
                    "FROM thread_controls WHERE thread = ?",
                    (thread_id,),
                ).fetchone()
                index = int(index_row["next_index"]) if index_row is not None else 0
                self._conn.execute(
                    """
                    INSERT INTO thread_controls(
                        thread, "index", kind, source_thread, anchor_run, result_run,
                        message, request_id, expected_head_thread, expected_head_index,
                        context, status, error, created_at, finished_at
                    ) VALUES (?, ?, 'rewind', NULL, ?, ?, ?, ?, ?, ?, ?,
                              'finished', NULL, ?, ?)
                    """,
                    (
                        thread_id,
                        index,
                        anchor_run,
                        result_run,
                        _dump_json(message.to_data()) if message is not None else None,
                        request_id,
                        expected_head.thread,
                        expected_head.index,
                        _dump_json(dict(context)),
                        created_at,
                        created_at,
                    ),
                )
                rows = self._conn.execute(
                    """
                    SELECT id FROM runs
                    WHERE thread = ? AND rowid >= ? AND superseded_by_thread IS NULL
                    ORDER BY rowid ASC
                    """,
                    (thread_id, int(anchor["rowid"])),
                ).fetchall()
                superseded = tuple(str(row["id"]) for row in rows)
                self._conn.executemany(
                    """
                    UPDATE runs
                    SET superseded_by_thread = ?, superseded_by_index = ?
                    WHERE id = ?
                    """,
                    ((thread_id, index, run_id) for run_id in superseded),
                )
                self._conn.execute(
                    """
                    UPDATE threads SET head_index = ?, updated_at = ?
                    WHERE thread_id = ? AND head_index = ?
                    """,
                    (index, created_at, thread_id, expected_head.index),
                )
                updated_thread = self._conn.execute(
                    "SELECT * FROM threads WHERE thread_id = ?", (thread_id,)
                ).fetchone()
                control_row = self._conn.execute(
                    'SELECT * FROM thread_controls WHERE thread = ? AND "index" = ?',
                    (thread_id, index),
                ).fetchone()
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        if updated_thread is None or control_row is None:
            raise RuntimeError(f"thread rewind failed: {thread_id}")
        return (
            _thread_from_row(updated_thread),
            _thread_control_from_row(control_row),
            superseded,
            True,
        )

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

    def get_thread_control(
        self, *, thread_id: str, index: int
    ) -> ThreadControlRecord | None:
        with self._lock:
            row = self._conn.execute(
                'SELECT * FROM thread_controls WHERE thread = ? AND "index" = ?',
                (thread_id, index),
            ).fetchone()
        return _thread_control_from_row(row) if row is not None else None

    def get_thread_control_by_request_id(
        self, *, request_id: str
    ) -> ThreadControlRecord | None:
        """Return the globally idempotent thread control for one request."""

        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM thread_controls WHERE request_id = ?",
                (request_id,),
            ).fetchone()
        return _thread_control_from_row(row) if row is not None else None

    def list_thread_controls(
        self, *, thread_id: str
    ) -> tuple[ThreadControlRecord, ...]:
        with self._lock:
            rows = self._conn.execute(
                'SELECT * FROM thread_controls WHERE thread = ? ORDER BY "index" ASC',
                (thread_id,),
            ).fetchall()
        return tuple(_thread_control_from_row(row) for row in rows)

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
                    _dump_json(output.to_data()) if output is not None else None,
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
            clauses.append("superseded_by_thread IS NULL")
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

    def list_runs_from_anchor(self, *, run_id: str) -> tuple[RunRecord, ...]:
        """Return visible runs accepted at or after one anchor in its thread."""

        with self._lock:
            anchor = self._conn.execute(
                "SELECT thread, rowid FROM runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            if anchor is None:
                raise ValueError(f"run not found: {run_id}")
            rows = self._conn.execute(
                """
                SELECT * FROM runs
                WHERE thread = ? AND rowid >= ? AND superseded_by_thread IS NULL
                ORDER BY rowid ASC
                """,
                (str(anchor["thread"]), int(anchor["rowid"])),
            ).fetchall()
        return tuple(_run_from_row(row) for row in rows)

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
            clauses.append("superseded_by_thread IS NULL")
        query = f"""
            SELECT * FROM runs
            WHERE {" AND ".join(clauses)}
            ORDER BY rowid ASC
        """
        if limit is not None:
            query = f"{query} LIMIT ?"
            params.append(limit)
        with self._lock:
            rows = self._conn.execute(query, tuple(params)).fetchall()
        return tuple(_run_from_row(row) for row in rows)

    def list_thread_history_chronological(
        self,
        *,
        thread_id: str,
        limit: int | None = None,
        include_superseded: bool = False,
    ) -> tuple[RunRecord, ...]:
        """Return projected history, following a fork's source prefix."""

        runs = self._thread_history(
            thread_id=thread_id,
            include_superseded=include_superseded,
            visited=set(),
        )
        return tuple(runs if limit is None else runs[-limit:])

    def _thread_history(
        self,
        *,
        thread_id: str,
        include_superseded: bool,
        visited: set[str],
    ) -> list[RunRecord]:
        if thread_id in visited:
            raise ValueError(f"thread fork cycle: {thread_id}")
        visited.add(thread_id)
        try:
            thread = self.get_thread(thread_id=thread_id)
            prefix: list[RunRecord] = []
            if thread is not None:
                control = self.get_thread_control(
                    thread_id=thread_id, index=thread.created_by.index
                )
                if (
                    control is not None
                    and control.kind == "fork"
                    and control.source_thread is not None
                    and control.anchor_run is not None
                ):
                    source = self._thread_history(
                        thread_id=control.source_thread,
                        include_superseded=True,
                        visited=visited,
                    )
                    for run in source:
                        prefix.append(run)
                        if run.id == control.anchor_run:
                            break
                    else:
                        raise ValueError(
                            f"fork anchor is missing from source history: {control.anchor_run}"
                        )
            own = self.list_thread_runs_chronological(
                thread_id=thread_id,
                limit=None,
                include_superseded=include_superseded,
            )
            return [*prefix, *own]
        finally:
            visited.remove(thread_id)

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
        prefix = _like_prefix(run_id)
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM steps
                WHERE parent = ? OR parent LIKE ? ESCAPE '\\'
                ORDER BY parent ASC, "index" ASC
                """,
                (run_id, f"{prefix}/%"),
            ).fetchall()
        return [_step_from_row(row) for row in rows]

    def list_steps_for_runs(
        self, *, run_ids: Sequence[str]
    ) -> dict[str, list[StepRecord]]:
        run_id_list = [item for item in run_ids if item]
        if not run_id_list:
            return {}
        clauses = " OR ".join(
            "(parent = ? OR parent LIKE ? ESCAPE '\\')" for _ in run_id_list
        )
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT * FROM steps
                WHERE {clauses}
                ORDER BY parent ASC, "index" ASC
                """,
                tuple(
                    item
                    for run_id in run_id_list
                    for item in (run_id, f"{_like_prefix(run_id)}/%")
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
            runs = [run for run in runs if run.id != exclude_run_id]
            runs = runs[-limit:]
        steps_by_run = self.list_steps_for_runs(
            run_ids=tuple(run.id for run in runs)
        )
        results: list[Message] = []
        for run in runs:
            inputs = self.list_run_controls(run_id=run.id)
            if inputs:
                results.extend(item.input for item in inputs if item.input is not None)
            for step in steps_by_run.get(run.id, ()):
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
            run_ids=tuple(run.id for run in runs)
        )
        results: list[Message] = []
        for run in runs:
            inputs = self.list_run_controls(run_id=run.id)
            input_messages = [item.input for item in inputs if item.input is not None]
            for input_message in input_messages:
                actor_message = _actor_text_message(input_message)
                if actor_message is not None:
                    results.append(actor_message)
            for step in steps_by_run.get(run.id, ()):
                for message in _replay_messages_from_step(step):
                    actor_message = _actor_text_message(message)
                    if actor_message is not None:
                        results.append(actor_message)
        return results[-limit:]

    def _conversation_runs(self, *, thread_id: str, limit: int) -> list[RunRecord]:
        current = list(
            self.list_thread_history_chronological(thread_id=thread_id, limit=None)
        )
        return current[-limit:]

    def append_update(
        self,
        *,
        kind: str,
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

    def get_run_control(self, *, run_id: str, index: int) -> RunControlRecord | None:
        with self._lock:
            row = self._conn.execute(
                'SELECT * FROM run_controls WHERE run = ? AND "index" = ?',
                (run_id, index),
            ).fetchone()
        return _run_control_from_row(row) if row is not None else None

    def list_run_controls(
        self,
        *,
        run_id: str,
        kind: RunControlKind | None = None,
    ) -> tuple[RunControlRecord, ...]:
        clauses = ["run = ?"]
        params: list[object] = [run_id]
        if kind is not None:
            clauses.append("kind = ?")
            params.append(kind)
        where = " AND ".join(clauses)
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT * FROM run_controls
                WHERE {where}
                ORDER BY "index" ASC
                """,
                tuple(params),
            ).fetchall()
        return tuple(_run_control_from_row(row) for row in rows)

    def pending_run_controls(
        self,
        *,
        run_id: str,
        kind: RunControlKind,
    ) -> tuple[RunControlRecord, ...]:
        """Return accepted run_controls not yet consumed by an execution event."""

        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM run_controls
                WHERE run = ? AND kind = ? AND status = 'pending'
                ORDER BY "index" ASC
                """,
                (run_id, kind),
            ).fetchall()
        return tuple(_run_control_from_row(row) for row in rows)

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.execute("PRAGMA busy_timeout=30000;")
            self._conn.execute("PRAGMA journal_mode=WAL;")
            self._conn.execute("PRAGMA synchronous=NORMAL;")
            self._conn.execute("PRAGMA foreign_keys=ON;")
            self._conn.execute("BEGIN IMMEDIATE")
            version = int(self._conn.execute("PRAGMA user_version").fetchone()[0])
            if version != _SCHEMA_VERSION:
                self._conn.execute("DROP TABLE IF EXISTS steps")
                self._conn.execute("DROP TABLE IF EXISTS run_control_counters")
                self._conn.execute("DROP TABLE IF EXISTS command_counters")
                self._conn.execute("DROP TABLE IF EXISTS thread_controls")
                self._conn.execute("DROP TABLE IF EXISTS run_controls")
                self._conn.execute("DROP TABLE IF EXISTS commands")
                self._conn.execute("DROP TABLE IF EXISTS inputs")
                self._conn.execute("DROP TABLE IF EXISTS runs")
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
                    created_by_index INTEGER NOT NULL,
                    head_index INTEGER NOT NULL,
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
                    superseded_by_thread TEXT,
                    superseded_by_index INTEGER,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS run_controls (
                    run TEXT NOT NULL,
                    "index" INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    timing TEXT NOT NULL,
                    input TEXT,
                    request_id TEXT,
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
                CREATE UNIQUE INDEX IF NOT EXISTS idx_run_controls_request
                ON run_controls(run, request_id)
                WHERE request_id IS NOT NULL
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS thread_controls (
                    thread TEXT NOT NULL,
                    "index" INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    source_thread TEXT,
                    anchor_run TEXT,
                    result_run TEXT,
                    message TEXT,
                    request_id TEXT,
                    expected_head_thread TEXT,
                    expected_head_index INTEGER,
                    context TEXT NOT NULL,
                    status TEXT NOT NULL,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    finished_at TEXT,
                    PRIMARY KEY(thread, "index")
                )
                """
            )
            self._conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_thread_controls_request
                ON thread_controls(request_id)
                WHERE request_id IS NOT NULL
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
            self._conn.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")
            self._conn.commit()


def run_store_path(toolang_root: Path, agent_name: str) -> Path:
    return toolang_root / "agents" / agent_name / ".runtime" / "runs.db"
def _dump_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _load_json(value: str) -> Any:
    return json.loads(value)


def _like_prefix(value: str) -> str:
    """Escape one literal prefix for a SQLite LIKE expression."""

    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _run_from_row(row: sqlite3.Row) -> RunRecord:
    context_raw = _load_json(str(row["context"])) if row["context"] is not None else {}
    output_raw = _load_json(str(row["output"])) if row["output"] is not None else None
    return RunRecord(
        id=str(row["id"]),
        parent=str(row["parent"]) if row["parent"] is not None else None,
        thread=str(row["thread"]),
        input=RunControlRef.from_data(
            cast(Mapping[str, Any], _load_json(str(row["input"])))
        ),
        output=(
            OutputRef.from_data(cast(Mapping[str, Any], output_raw))
            if isinstance(output_raw, Mapping)
            else None
        ),
        context=dict(context_raw) if isinstance(context_raw, Mapping) else {},
        status=cast(RunStatus, row["status"]),
        error=str(row["error"]) if row["error"] is not None else None,
        superseded_by=(
            ThreadControlRef(
                thread=str(row["superseded_by_thread"]),
                index=int(row["superseded_by_index"]),
            )
            if row["superseded_by_thread"] is not None
            and row["superseded_by_index"] is not None
            else None
        ),
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
        created_by=ThreadControlRef(
            thread=str(raw["thread_id"]),
            index=int(cast(int | str, raw["created_by_index"])),
        ),
        head=ThreadControlRef(
            thread=str(raw["thread_id"]),
            index=int(cast(int | str, raw["head_index"])),
        ),
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
        kind=str(row["kind"]),
        payload=dict(payload_raw) if isinstance(payload_raw, Mapping) else {},
        created_at=str(row["created_at"]),
    )


def _run_control_from_row(row: sqlite3.Row) -> RunControlRecord:
    input_raw = _load_json(str(row["input"])) if row["input"] is not None else None
    context_raw = _load_json(str(row["context"])) if row["context"] is not None else {}
    return RunControlRecord(
        run=str(row["run"]),
        index=int(row["index"]),
        kind=cast(RunControlKind, row["kind"]),
        timing=cast(RunControlTiming, row["timing"]),
        input=Message.from_data(input_raw) if isinstance(input_raw, Mapping) else None,
        request_id=str(row["request_id"]) if row["request_id"] is not None else None,
        context=dict(context_raw) if isinstance(context_raw, Mapping) else {},
        status=row["status"],
        error=str(row["error"]) if row["error"] is not None else None,
        created_at=str(row["created_at"]),
        finished_at=str(row["finished_at"]) if row["finished_at"] is not None else None,
    )


def _thread_control_from_row(row: sqlite3.Row) -> ThreadControlRecord:
    message_raw = (
        _load_json(str(row["message"])) if row["message"] is not None else None
    )
    context_raw = _load_json(str(row["context"])) if row["context"] is not None else {}
    expected_head = (
        ThreadControlRef(
            thread=str(row["expected_head_thread"]),
            index=int(row["expected_head_index"]),
        )
        if row["expected_head_thread"] is not None
        and row["expected_head_index"] is not None
        else None
    )
    return ThreadControlRecord(
        thread=str(row["thread"]),
        index=int(row["index"]),
        kind=row["kind"],
        source_thread=(
            str(row["source_thread"]) if row["source_thread"] is not None else None
        ),
        anchor_run=str(row["anchor_run"]) if row["anchor_run"] is not None else None,
        result_run=str(row["result_run"]) if row["result_run"] is not None else None,
        message=Message.from_data(message_raw)
        if isinstance(message_raw, Mapping)
        else None,
        request_id=str(row["request_id"]) if row["request_id"] is not None else None,
        expected_head=expected_head,
        context=dict(context_raw) if isinstance(context_raw, Mapping) else {},
        status=row["status"],
        error=str(row["error"]) if row["error"] is not None else None,
        created_at=str(row["created_at"]),
        finished_at=str(row["finished_at"]) if row["finished_at"] is not None else None,
    )


def _replay_messages_from_step(step: StepRecord) -> list[Message]:
    role = step_message_role(step.kind)
    if role is None or not step.output:
        return []
    meta: dict[str, Any] = {}
    if step.error is not None:
        meta["error"] = step.error
    reasoning_content = step.detail.get("reasoning_content")
    if isinstance(reasoning_content, str) and reasoning_content:
        meta["reasoning_content"] = reasoning_content
    return [Message(role=role, parts=tuple(step.output), meta=meta)]
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
