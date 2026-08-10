"""SQLite-backed durable execution truth."""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
from typing import Any, Literal, cast

from toolang.base.types.message import (
    Message,
    MessagePart,
    ToolCallPart,
    ToolResultPart,
    message_text,
    parts_from_data,
    parts_to_data,
)
from toolang.base.types.run import ModelCall
from toolang.base.types.tool import ToolDefinition
from toolang.common.time import utc_now
from .errors import RunStoreSchemaError
from .records import (
    ValueRef,
    value_ref_from_data,
    value_ref_to_data,
    RunControlRecord,
    RunControlRef,
    RunInputRef,
    RunRecord,
    StepInput,
    StepRecord,
    ThreadPeer,
    ThreadControlRecord,
    ThreadControlRef,
    ThreadRecord,
    step_message_role,
    step_inputs_from_data,
    step_inputs_to_data,
)
from .types import (
    RunControlKind,
    ControlTiming,
    RunStatus,
    StepKind,
    StepStatus,
    StepPath,
)

_SCHEMA_VERSION = 21
_MIGRATABLE_SCHEMA_VERSIONS = (13, 14, 15, 16, 17, 18, 19, 20, _SCHEMA_VERSION)


class RunStore:
    """Durable thread and run truth for one agent."""

    def __init__(self, db_path: Path, *, read_only: bool = False) -> None:
        self.db_path = db_path
        self.read_only = read_only
        if read_only:
            target = f"{db_path.expanduser().resolve().as_uri()}?mode=ro"
            self._conn = sqlite3.connect(
                target,
                uri=True,
                check_same_thread=False,
                timeout=30,
            )
        else:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(
                db_path.as_posix(),
                check_same_thread=False,
                timeout=30,
            )
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        try:
            self._init_schema()
        except BaseException:
            self._conn.close()
            raise

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def write_transaction(self) -> Iterator[None]:
        """Commit one durable write unit, joining an existing store transaction."""

        with self._lock:
            owner = not self._conn.in_transaction
            if owner:
                self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                if owner:
                    self._conn.rollback()
                raise
            else:
                if owner:
                    self._conn.commit()

    @property
    def thread_lock_path(self) -> Path:
        """Return the shared lock that serializes thread history mutations."""

        return self.db_path.with_name(f"{self.db_path.name}.threads.lock")

    def accept_start(
        self,
        *,
        run_id: str,
        parent: StepPath | None,
        thread: str,
        input: Message,
        context: Mapping[str, Any],
        request_id: str | None,
        created_at: str,
        control_context: Mapping[str, Any] | None = None,
        kind: Literal["start", "rerun"] = "start",
        source: str | None = None,
    ) -> tuple[RunRecord, RunControlRecord]:
        """Atomically insert one new run and its start control."""

        if not run_id or "/" in run_id:
            raise ValueError(f"invalid run id: {run_id!r}")
        if kind == "start" and source is not None:
            raise ValueError("start control cannot have a source run")
        if kind == "rerun" and source is None:
            raise ValueError("rerun control requires a source run")
        _validate_request_id(request_id)

        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                if (
                    self._conn.execute(
                        "SELECT 1 FROM runs WHERE id = ?", (run_id,)
                    ).fetchone()
                    is not None
                ):
                    raise ValueError(f"run already exists: {run_id}")
                if request_id is not None and (
                    self._conn.execute(
                        "SELECT 1 FROM run_controls WHERE request_id = ?",
                        (request_id,),
                    ).fetchone()
                    is not None
                ):
                    raise ValueError(
                        f"run control request already exists: {request_id}"
                    )
                if (
                    self._conn.execute(
                        "SELECT 1 FROM threads WHERE thread_id = ?", (thread,)
                    ).fetchone()
                    is None
                ):
                    raise ValueError(f"thread not found: {thread}")
                if kind == "rerun":
                    source_row = self._conn.execute(
                        """
                        SELECT * FROM runs
                        WHERE id = ? AND parent IS NULL
                        """,
                        (source,),
                    ).fetchone()
                    if source_row is None:
                        raise ValueError(f"source root run not found: {source}")
                    source_record = _run_from_row(source_row)
                    if source_record.thread != thread:
                        raise ValueError(
                            "rerun source must belong to the target thread"
                        )
                    if source_record.ejected is not None:
                        raise ValueError(f"rerun source is not visible: {source}")
                    if source_record.status not in {"finished", "failed", "canceled"}:
                        raise ValueError(f"rerun source is not terminal: {source}")
                    latest = self._conn.execute(
                        """
                        SELECT id FROM runs
                        WHERE thread = ? AND parent IS NULL
                          AND ejected_thread IS NULL AND ejected_run IS NULL
                        ORDER BY rowid DESC LIMIT 1
                        """,
                        (thread,),
                    ).fetchone()
                    if latest is None or str(latest["id"]) != source:
                        raise ValueError(
                            "rerun source must be the latest visible root run"
                        )
                self._conn.execute(
                    """
                    INSERT INTO runs(
                        id, parent, thread, input, output, context, status, error,
                        ejected_thread, ejected_run, ejected_index,
                        created_at, started_at, finished_at
                    ) VALUES (
                        ?, ?, ?, ?, NULL, ?, 'pending', NULL,
                        NULL, NULL, NULL, ?, NULL, NULL
                    )
                    """,
                    (
                        run_id,
                        str(parent) if parent is not None else None,
                        thread,
                        _dump_json(RunInputRef(index=0).to_data()),
                        _dump_json(dict(context)),
                        created_at,
                    ),
                )
                self._conn.execute(
                    """
                    INSERT INTO run_controls(
                        run, "index", kind, timing, input, source, anchor,
                        request_id, context,
                        status, error, created_at, finished_at, revision
                    ) VALUES (
                        ?, 0, ?, 'immediate', ?, ?, NULL, ?, ?,
                        'pending', NULL, ?, NULL, ?
                    )
                    """,
                    (
                        run_id,
                        kind,
                        _dump_json(input.to_data()),
                        source,
                        request_id,
                        _dump_json(
                            dict(context)
                            if control_context is None
                            else dict(control_context)
                        ),
                        created_at,
                        self._next_run_control_revision(),
                    ),
                )
                if kind == "rerun":
                    source_tree = self._root_tree_runs(cast(str, source))
                    self._conn.executemany(
                        """
                        UPDATE runs
                        SET ejected_thread = NULL, ejected_run = ?, ejected_index = 0
                        WHERE id = ?
                        """,
                        ((run_id, source_run) for source_run in source_tree),
                    )
                run_row = self._conn.execute(
                    "SELECT * FROM runs WHERE id = ?", (run_id,)
                ).fetchone()
                control_row = self._conn.execute(
                    'SELECT * FROM run_controls WHERE run = ? AND "index" = 0',
                    (run_id,),
                ).fetchone()
                self._conn.commit()
            except sqlite3.IntegrityError as exc:
                self._conn.rollback()
                identity = request_id or run_id
                raise ValueError(f"run control already exists: {identity}") from exc
            except Exception:
                self._conn.rollback()
                raise
        if run_row is None or control_row is None:
            raise RuntimeError(f"run acceptance failed: {run_id}")
        return _run_from_row(run_row), _run_control_from_row(control_row)

    def accept_run_control(
        self,
        *,
        run_id: str,
        kind: RunControlKind,
        timing: ControlTiming,
        input: Message | None,
        context: Mapping[str, Any],
        request_id: str | None,
        created_at: str,
    ) -> RunControlRecord:
        """Atomically allocate and accept one steer or stop control."""

        if kind not in {"steer", "stop"}:
            raise ValueError(f"unsupported run control kind: {kind}")
        if timing not in {"immediate", "next_step", "next_call"}:
            raise ValueError(f"unsupported run control timing: {timing}")
        if kind == "steer" and input is None:
            raise ValueError("steer control requires input")
        _validate_request_id(request_id)

        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                if request_id is not None and (
                    self._conn.execute(
                        "SELECT 1 FROM run_controls WHERE request_id = ?",
                        (request_id,),
                    ).fetchone()
                    is not None
                ):
                    raise ValueError(
                        f"run control request already exists: {request_id}"
                    )
                run = self._conn.execute(
                    "SELECT status FROM runs WHERE id = ?", (run_id,)
                ).fetchone()
                if run is None:
                    raise ValueError(f"run not found: {run_id}")
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
                        run, "index", kind, timing, input, source, anchor,
                        request_id, context,
                        status, error, created_at, finished_at, revision
                    ) VALUES (
                        ?, ?, ?, ?, ?, NULL, NULL, ?, ?,
                        'pending', NULL, ?, NULL, ?
                    )
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
                        self._next_run_control_revision(),
                    ),
                )
                inserted = self._conn.execute(
                    'SELECT * FROM run_controls WHERE run = ? AND "index" = ?',
                    (run_id, index),
                ).fetchone()
                self._conn.commit()
            except sqlite3.IntegrityError as exc:
                self._conn.rollback()
                identity = request_id or f"{run_id}:{index}"
                raise ValueError(f"run control already exists: {identity}") from exc
            except Exception:
                self._conn.rollback()
                raise
        if inserted is None:
            raise RuntimeError(f"run control acceptance failed: {run_id}")
        return _run_control_from_row(inserted)

    def accept_retry(
        self,
        *,
        run_id: str,
        anchor: StepPath | None,
        context: Mapping[str, Any],
        request_id: str | None,
        created_at: str,
    ) -> tuple[RunRecord, RunControlRecord, tuple[StepPath, ...]]:
        """Atomically cut one root run at a step and reopen it for execution."""

        _validate_request_id(request_id)
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                if request_id is not None and (
                    self._conn.execute(
                        "SELECT 1 FROM run_controls WHERE request_id = ?",
                        (request_id,),
                    ).fetchone()
                    is not None
                ):
                    raise ValueError(
                        f"run control request already exists: {request_id}"
                    )
                run_row = self._conn.execute(
                    "SELECT * FROM runs WHERE id = ? AND parent IS NULL",
                    (run_id,),
                ).fetchone()
                if run_row is None:
                    raise ValueError(f"root run not found: {run_id}")
                run = _run_from_row(run_row)
                if run.ejected is not None:
                    raise ValueError(f"run is not visible: {run_id}")
                if run.status not in {"finished", "failed", "canceled"}:
                    raise ValueError(f"run is not terminal: {run_id}")
                tree_runs = self._root_tree_runs(run_id)
                resolved_anchor = self._resolve_retry_anchor(
                    run_id=run_id,
                    tree_runs=tree_runs,
                    anchor=anchor,
                )
                index_row = self._conn.execute(
                    'SELECT COALESCE(MAX("index"), -1) + 1 AS next_index '
                    "FROM run_controls WHERE run = ?",
                    (run_id,),
                ).fetchone()
                index = int(index_row["next_index"]) if index_row is not None else 1
                ejected = self._retry_step_suffix(
                    tree_runs=tree_runs,
                    anchor=resolved_anchor,
                )
                self._conn.execute(
                    """
                    INSERT INTO run_controls(
                        run, "index", kind, timing, input, source, anchor,
                        request_id, context, status, error, created_at, finished_at,
                        claimed, revision
                    ) VALUES (
                        ?, ?, 'retry', 'immediate', NULL, NULL, ?, ?, ?,
                        'finished', NULL, ?, ?, 1, ?
                    )
                    """,
                    (
                        run_id,
                        index,
                        str(resolved_anchor),
                        request_id,
                        _dump_json(dict(context)),
                        created_at,
                        created_at,
                        self._next_run_control_revision(),
                    ),
                )
                self._conn.executemany(
                    """
                    UPDATE steps
                    SET ejected_run = ?, ejected_index = ?
                    WHERE run = ? AND path = ? AND ejected_run IS NULL
                    """,
                    ((run_id, index, path.run, path.local) for path in ejected),
                )
                self._conn.execute(
                    """
                    UPDATE run_controls
                    SET status = 'failed',
                        error = 'run retried before the control could be applied',
                        finished_at = ?,
                        revision = ?
                    WHERE run IN ({}) AND status = 'pending'
                    """.format(", ".join("?" for _ in tree_runs)),
                    (
                        created_at,
                        self._next_run_control_revision(),
                        *tree_runs,
                    ),
                )
                self._conn.execute(
                    """
                    UPDATE runs
                    SET status = 'pending', output = NULL, error = NULL,
                        started_at = NULL, finished_at = NULL
                    WHERE id = ?
                    """,
                    (run_id,),
                )
                updated_run_row = self._conn.execute(
                    "SELECT * FROM runs WHERE id = ?", (run_id,)
                ).fetchone()
                control_row = self._conn.execute(
                    'SELECT * FROM run_controls WHERE run = ? AND "index" = ?',
                    (run_id, index),
                ).fetchone()
                self._conn.commit()
            except sqlite3.IntegrityError as exc:
                self._conn.rollback()
                identity = request_id or run_id
                raise ValueError(f"run retry already exists: {identity}") from exc
            except Exception:
                self._conn.rollback()
                raise
        if updated_run_row is None or control_row is None:
            raise RuntimeError(f"run retry acceptance failed: {run_id}")
        return (
            _run_from_row(updated_run_row),
            _run_control_from_row(control_row),
            ejected,
        )

    def begin_run(
        self,
        *,
        run_id: str,
        started_at: str,
        context: Mapping[str, Any] | None = None,
    ) -> RunRecord:
        """Project run execution beginning."""

        with self.write_transaction():
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
        with self.write_transaction():
            pending = self._conn.execute(
                f"""
                SELECT 1 FROM run_controls
                WHERE run = ? AND "index" IN ({placeholders})
                  AND status = 'pending'
                LIMIT 1
                """,
                (run_id, *control_indexes),
            ).fetchone()
            if pending is None:
                return
            self._conn.execute(
                f"""
                UPDATE run_controls
                SET status = 'finished', finished_at = ?, revision = ?
                WHERE run = ? AND "index" IN ({placeholders}) AND status = 'pending'
                """,
                (
                    finished_at,
                    self._next_run_control_revision(),
                    run_id,
                    *control_indexes,
                ),
            )

    def fail_pending_run_controls(
        self, *, run_id: str, finished_at: str, error: str
    ) -> None:
        """Fail controls that can no longer be applied to a terminal run."""

        with self.write_transaction():
            pending = self._conn.execute(
                """
                SELECT 1 FROM run_controls
                WHERE run = ? AND status = 'pending'
                LIMIT 1
                """,
                (run_id,),
            ).fetchone()
            if pending is None:
                return
            self._conn.execute(
                """
                UPDATE run_controls
                SET status = 'failed', error = ?, finished_at = ?, revision = ?
                WHERE run = ? AND status = 'pending'
                """,
                (
                    error,
                    finished_at,
                    self._next_run_control_revision(),
                    run_id,
                ),
            )

    def cancel_run_control(
        self,
        *,
        run_id: str,
        index: int,
        canceled_at: str,
    ) -> RunControlRecord:
        """Cancel one pending steer or stop control."""

        with self.write_transaction():
            row = self._conn.execute(
                'SELECT * FROM run_controls WHERE run = ? AND "index" = ?',
                (run_id, index),
            ).fetchone()
            if row is None:
                raise ValueError(f"run control not found: {run_id}:{index}")
            control = _run_control_from_row(row)
            if control.kind in {"start", "rerun"}:
                raise ValueError("run entry controls cannot be canceled")
            if control.status != "pending":
                raise ValueError(f"run control is not pending: {run_id}:{index}")
            if bool(row["claimed"]):
                raise ValueError(
                    f"run control is already being applied: {run_id}:{index}"
                )
            self._conn.execute(
                """
                UPDATE run_controls
                SET status = 'canceled', finished_at = ?, revision = ?
                WHERE run = ? AND "index" = ? AND status = 'pending'
                """,
                (
                    canceled_at,
                    self._next_run_control_revision(),
                    run_id,
                    index,
                ),
            )
            updated = self._conn.execute(
                'SELECT * FROM run_controls WHERE run = ? AND "index" = ?',
                (run_id, index),
            ).fetchone()
        if updated is None:
            raise RuntimeError(f"run control cancellation failed: {run_id}:{index}")
        return _run_control_from_row(updated)

    def claim_run_controls(
        self,
        *,
        run_id: str,
        indexes: Sequence[int],
    ) -> set[int]:
        """Atomically claim pending controls before runtime application."""

        control_indexes = tuple(dict.fromkeys(int(index) for index in indexes))
        if not control_indexes:
            return set()
        placeholders = ", ".join("?" for _ in control_indexes)
        with self.write_transaction():
            self._conn.execute(
                f"""
                UPDATE run_controls
                SET claimed = 1
                WHERE run = ? AND "index" IN ({placeholders})
                  AND status = 'pending'
                """,
                (run_id, *control_indexes),
            )
            rows = self._conn.execute(
                f"""
                SELECT "index" FROM run_controls
                WHERE run = ? AND "index" IN ({placeholders})
                  AND status = 'pending' AND claimed = 1
                """,
                (run_id, *control_indexes),
            ).fetchall()
        return {int(row["index"]) for row in rows}

    def create_thread(
        self,
        *,
        thread_id: str,
        origin: str = "chat",
        peer: ThreadPeer | None = None,
        request_id: str | None = None,
        context: Mapping[str, Any] | None = None,
        created_at: str | None = None,
    ) -> tuple[ThreadRecord, ThreadControlRecord]:
        """Atomically create one thread and its create control."""

        _validate_request_id(request_id)
        now = created_at or utc_now()
        effective_peer = peer or ThreadPeer()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                if request_id is not None and (
                    self._conn.execute(
                        "SELECT 1 FROM thread_controls WHERE request_id = ?",
                        (request_id,),
                    ).fetchone()
                    is not None
                ):
                    raise ValueError(
                        f"thread control request already exists: {request_id}"
                    )
                existing_thread = self._conn.execute(
                    "SELECT * FROM threads WHERE thread_id = ?", (thread_id,)
                ).fetchone()
                if existing_thread is not None:
                    raise ValueError(f"thread already exists: {thread_id}")
                self._conn.execute(
                    """
                    INSERT INTO thread_controls(
                        thread, "index", kind, source, anchor,
                        request_id, expected_head_thread, expected_head_index,
                        context, status, created_at, finished_at
                    ) VALUES (?, 0, 'create', NULL, NULL, ?, NULL, NULL,
                              ?, 'finished', ?, ?)
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
            except sqlite3.IntegrityError as exc:
                self._conn.rollback()
                identity = request_id or thread_id
                raise ValueError(f"thread control already exists: {identity}") from exc
            except Exception:
                self._conn.rollback()
                raise
        if thread_row is None or control_row is None:
            raise RuntimeError(f"thread creation failed: {thread_id}")
        return _thread_from_row(thread_row), _thread_control_from_row(control_row)

    def fork_thread(
        self,
        *,
        thread_id: str,
        source: str,
        anchor: str | None,
        request_id: str | None,
        context: Mapping[str, Any],
        created_at: str,
    ) -> tuple[ThreadRecord, ThreadControlRecord]:
        """Atomically fork from one terminal anchor without copying runs."""

        _validate_request_id(request_id)
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                if request_id is not None and (
                    self._conn.execute(
                        "SELECT 1 FROM thread_controls WHERE request_id = ?",
                        (request_id,),
                    ).fetchone()
                    is not None
                ):
                    raise ValueError(
                        f"thread control request already exists: {request_id}"
                    )
                source_row = self._conn.execute(
                    "SELECT * FROM threads WHERE thread_id = ?",
                    (source,),
                ).fetchone()
                if source_row is None:
                    raise ValueError(f"thread not found: {source}")
                source_record = _thread_from_row(source_row)
                anchor_record = self._resolve_thread_anchor(
                    thread_id=source_record.thread_id,
                    run_id=anchor,
                    require_idle=False,
                )
                if (
                    self._conn.execute(
                        "SELECT 1 FROM threads WHERE thread_id = ?", (thread_id,)
                    ).fetchone()
                    is not None
                ):
                    raise ValueError(f"thread already exists: {thread_id}")
                self._conn.execute(
                    """
                    INSERT INTO thread_controls(
                        thread, "index", kind, source, anchor,
                        request_id, expected_head_thread, expected_head_index,
                        context, status, created_at, finished_at
                    ) VALUES (?, 0, 'fork', ?, ?, ?, NULL, NULL, ?,
                              'finished', ?, ?)
                    """,
                    (
                        thread_id,
                        source_record.thread_id,
                        anchor_record.id,
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
                        source_record.origin,
                        _dump_json(source_record.peer.to_data()),
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
            except sqlite3.IntegrityError as exc:
                self._conn.rollback()
                identity = request_id or thread_id
                raise ValueError(f"thread control already exists: {identity}") from exc
            except Exception:
                self._conn.rollback()
                raise
        if thread_row is None or control_row is None:
            raise RuntimeError(f"thread fork failed: {thread_id}")
        return _thread_from_row(thread_row), _thread_control_from_row(control_row)

    def rewind_thread(
        self,
        *,
        thread_id: str,
        anchor: str | None,
        request_id: str | None,
        expected_head: ThreadControlRef,
        context: Mapping[str, Any],
        created_at: str,
    ) -> tuple[ThreadRecord, ThreadControlRecord, tuple[str, ...]]:
        """Atomically rewind one idle thread using optimistic head comparison."""

        _validate_request_id(request_id)
        index: int | None = None
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                if request_id is not None and (
                    self._conn.execute(
                        "SELECT 1 FROM thread_controls WHERE request_id = ?",
                        (request_id,),
                    ).fetchone()
                    is not None
                ):
                    raise ValueError(
                        f"thread control request already exists: {request_id}"
                    )
                thread_row = self._conn.execute(
                    "SELECT * FROM threads WHERE thread_id = ?", (thread_id,)
                ).fetchone()
                if thread_row is None:
                    raise ValueError(f"thread not found: {thread_id}")
                thread = _thread_from_row(thread_row)
                if thread.head != expected_head:
                    raise ValueError(f"thread head changed: {thread_id}")
                anchor_record = self._resolve_thread_anchor(
                    thread_id=thread_id,
                    run_id=anchor,
                    require_idle=True,
                )
                anchor = self._conn.execute(
                    "SELECT rowid, thread FROM runs WHERE id = ?",
                    (anchor_record.id,),
                ).fetchone()
                if anchor is None:
                    raise ValueError(f"run not found: {anchor_record.id}")
                index_row = self._conn.execute(
                    'SELECT COALESCE(MAX("index"), -1) + 1 AS next_index '
                    "FROM thread_controls WHERE thread = ?",
                    (thread_id,),
                ).fetchone()
                index = int(index_row["next_index"]) if index_row is not None else 0
                self._conn.execute(
                    """
                    INSERT INTO thread_controls(
                        thread, "index", kind, source, anchor,
                        request_id, expected_head_thread, expected_head_index,
                        context, status, created_at, finished_at
                    ) VALUES (?, ?, 'rewind', NULL, ?, ?, ?, ?, ?,
                              'finished', ?, ?)
                    """,
                    (
                        thread_id,
                        index,
                        anchor_record.id,
                        request_id,
                        expected_head.thread,
                        expected_head.index,
                        _dump_json(dict(context)),
                        created_at,
                        created_at,
                    ),
                )
                if str(anchor["thread"]) == thread_id:
                    rows = self._conn.execute(
                        """
                        SELECT id FROM runs
                        WHERE thread = ?
                          AND rowid >= ?
                          AND ejected_thread IS NULL
                          AND ejected_run IS NULL
                        ORDER BY rowid ASC
                        """,
                        (thread_id, int(anchor["rowid"])),
                    ).fetchall()
                else:
                    rows = self._conn.execute(
                        """
                        SELECT id FROM runs
                        WHERE thread = ?
                          AND ejected_thread IS NULL
                          AND ejected_run IS NULL
                        ORDER BY rowid ASC
                        """,
                        (thread_id,),
                    ).fetchall()
                ejected = tuple(str(row["id"]) for row in rows)
                self._conn.executemany(
                    """
                    UPDATE runs
                    SET ejected_thread = ?, ejected_run = NULL, ejected_index = ?
                    WHERE id = ?
                    """,
                    ((thread_id, index, run_id) for run_id in ejected),
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
            except sqlite3.IntegrityError as exc:
                self._conn.rollback()
                identity = request_id or (
                    f"{thread_id}:{index}" if index is not None else thread_id
                )
                raise ValueError(f"thread control already exists: {identity}") from exc
            except Exception:
                self._conn.rollback()
                raise
        if updated_thread is None or control_row is None:
            raise RuntimeError(f"thread rewind failed: {thread_id}")
        return (
            _thread_from_row(updated_thread),
            _thread_control_from_row(control_row),
            ejected,
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
        output: ValueRef | None = None,
    ) -> RunRecord:
        now = finished_at or utc_now()
        with self.write_transaction():
            self._conn.execute(
                """
                UPDATE runs
                SET status = ?, error = ?, output = ?, finished_at = ?
                WHERE id = ? AND status IN ('pending', 'running')
                """,
                (
                    status,
                    error,
                    _dump_json(value_ref_to_data(output))
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

    def run_output(self, *, run_id: str) -> tuple[MessagePart, ...]:
        """Return the parts referenced by one run's durable output edge."""

        run = self.get_run(run_id=run_id)
        if run is None:
            raise ValueError(f"run not found: {run_id}")
        if run.output is None:
            return ()
        if isinstance(run.output, RunInputRef):
            control = self.get_run_control(run_id=run_id, index=run.output.index)
            if control is None or control.input is None:
                return ()
            parts = control.input.parts
            if run.output.part is None:
                return parts
            if 0 <= run.output.part < len(parts):
                return (parts[run.output.part],)
            return ()
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM steps WHERE run = ? AND path = ?",
                (run.output.step.run, run.output.step.local),
            ).fetchone()
        return run.output.resolve((_step_from_row(row),)) if row is not None else ()

    def run_output_text(self, *, run_id: str) -> str:
        """Return the text projection of one run's durable output."""

        return message_text(self.run_output(run_id=run_id))

    def list_runs(
        self,
        *,
        limit: int | None = 50,
        thread_id: str | None = None,
        status: RunStatus | None = None,
        include_ejected: bool = False,
    ) -> list[RunRecord]:
        clauses: list[str] = []
        params: list[object] = []
        if thread_id is not None:
            clauses.append("thread = ?")
            params.append(thread_id)
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        if not include_ejected:
            clauses.extend(
                (
                    "ejected_thread IS NULL",
                    "ejected_run IS NULL",
                    "(parent IS NULL OR parent NOT IN ("
                    "SELECT run || '/' || path FROM steps "
                    "WHERE ejected_run IS NOT NULL))",
                )
            )
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        query = f"SELECT * FROM runs {where} ORDER BY created_at DESC"
        if limit is not None:
            query = f"{query} LIMIT ?"
            params.append(limit)
        with self._lock:
            rows = self._conn.execute(query, tuple(params)).fetchall()
        return [_run_from_row(row) for row in rows]

    def list_thread_runs_chronological(
        self,
        *,
        thread_id: str,
        limit: int | None = None,
        include_ejected: bool = False,
    ) -> tuple[RunRecord, ...]:
        """Return one thread's runs in durable chronological order."""

        clauses = ["thread = ?"]
        params: list[object] = [thread_id]
        if not include_ejected:
            clauses.extend(
                (
                    "ejected_thread IS NULL",
                    "ejected_run IS NULL",
                    "(parent IS NULL OR parent NOT IN ("
                    "SELECT run || '/' || path FROM steps "
                    "WHERE ejected_run IS NOT NULL))",
                )
            )
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
        include_ejected: bool = False,
    ) -> tuple[RunRecord, ...]:
        """Return projected history, following a fork's source prefix."""

        return self.list_thread_histories_chronological(
            thread_ids=(thread_id,),
            limit=limit,
            include_ejected=include_ejected,
        ).get(thread_id, ())

    def list_thread_histories_chronological(
        self,
        *,
        thread_ids: Sequence[str],
        limit: int | None = None,
        include_ejected: bool = False,
    ) -> dict[str, tuple[RunRecord, ...]]:
        """Return projected histories for several threads from one store snapshot."""

        selected = tuple(dict.fromkeys(item for item in thread_ids if item))
        if not selected:
            return {}
        with self._lock:
            owner = not self._conn.in_transaction
            if owner:
                self._conn.execute("BEGIN")
            try:
                thread_rows = self._conn.execute("SELECT * FROM threads").fetchall()
                control_rows = self._conn.execute(
                    'SELECT * FROM thread_controls ORDER BY thread ASC, "index" ASC'
                ).fetchall()
                run_rows = self._conn.execute(
                    "SELECT * FROM runs ORDER BY rowid ASC"
                ).fetchall()
                ejected_step_rows = self._conn.execute(
                    "SELECT run, path FROM steps WHERE ejected_run IS NOT NULL"
                ).fetchall()
                if owner:
                    self._conn.commit()
            except BaseException:
                if owner:
                    self._conn.rollback()
                raise
        threads = {
            record.thread_id: record
            for record in (_thread_from_row(row) for row in thread_rows)
        }
        controls_by_thread: dict[str, list[ThreadControlRecord]] = {}
        for row in control_rows:
            control = _thread_control_from_row(row)
            controls_by_thread.setdefault(control.thread, []).append(control)
        runs_by_thread: dict[str, list[RunRecord]] = {}
        for row in run_rows:
            run = _run_from_row(row)
            runs_by_thread.setdefault(run.thread, []).append(run)
        ejected_steps = {f"{row['run']}/{row['path']}" for row in ejected_step_rows}
        cache: dict[tuple[str, bool], tuple[RunRecord, ...]] = {}

        def history(
            thread_id: str,
            *,
            include_hidden: bool,
            visited: set[str],
        ) -> list[RunRecord]:
            key = (thread_id, include_hidden)
            cached = cache.get(key)
            if cached is not None:
                return list(cached)
            if thread_id in visited:
                raise ValueError(f"thread fork cycle: {thread_id}")
            visited.add(thread_id)
            try:
                thread = threads.get(thread_id)
                controls = controls_by_thread.get(thread_id, ())
                prefix: list[RunRecord] = []
                if thread is not None:
                    created_by = next(
                        (
                            control
                            for control in controls
                            if control.index == thread.created_by.index
                        ),
                        None,
                    )
                    if (
                        created_by is not None
                        and created_by.kind == "fork"
                        and created_by.source is not None
                        and created_by.anchor is not None
                    ):
                        source = history(
                            created_by.source,
                            include_hidden=True,
                            visited=visited,
                        )
                        for run in source:
                            prefix.append(run)
                            if run.id == created_by.anchor:
                                break
                        else:
                            raise ValueError(
                                "fork anchor is missing from source history: "
                                f"{created_by.anchor}"
                            )
                    if prefix:
                        positions = {
                            run.id: position for position, run in enumerate(prefix)
                        }
                        cuts = tuple(
                            positions[control.anchor]
                            for control in controls
                            if control.kind == "rewind" and control.anchor in positions
                        )
                        if cuts:
                            prefix = prefix[: min(cuts)]
                own = [
                    run
                    for run in runs_by_thread.get(thread_id, ())
                    if include_hidden
                    or (
                        run.ejected is None
                        and (run.parent is None or str(run.parent) not in ejected_steps)
                    )
                ]
                result = [*prefix, *own]
                cache[key] = tuple(result)
                return result
            finally:
                visited.remove(thread_id)

        result: dict[str, tuple[RunRecord, ...]] = {}
        for thread_id in selected:
            runs = history(
                thread_id,
                include_hidden=include_ejected,
                visited=set(),
            )
            result[thread_id] = _history_tail(runs, limit=limit)
        return result

    def _resolve_thread_anchor(
        self,
        *,
        thread_id: str,
        run_id: str | None,
        require_idle: bool,
    ) -> RunRecord:
        """Resolve one visible terminal root run inside a write transaction."""

        history = tuple(
            run
            for run in self.list_thread_history_chronological(
                thread_id=thread_id,
                include_ejected=False,
            )
            if run.parent is None
        )
        if not history:
            raise ValueError(f"thread has no runs: {thread_id}")
        if require_idle and any(
            run.status in {"pending", "running"} for run in history
        ):
            raise ValueError(f"thread is running: {thread_id}")
        anchor = (
            history[-1]
            if run_id is None
            else next((run for run in history if run.id == run_id), None)
        )
        if anchor is None:
            raise ValueError(f"run is not visible in thread {thread_id}: {run_id}")
        if anchor.status not in {"finished", "failed", "canceled"}:
            raise ValueError(f"anchor run is not terminal: {anchor.id}")
        return anchor

    def _root_tree_runs(self, root_run_id: str) -> tuple[str, ...]:
        """Return every run structurally owned by one root run."""

        rows = self._conn.execute("SELECT * FROM runs ORDER BY rowid ASC").fetchall()
        records = [_run_from_row(row) for row in rows]
        selected = {root_run_id}
        changed = True
        while changed:
            changed = False
            for run in records:
                if (
                    run.id not in selected
                    and run.parent is not None
                    and run.parent.run in selected
                ):
                    selected.add(run.id)
                    changed = True
        return tuple(run.id for run in records if run.id in selected)

    def _resolve_retry_anchor(
        self,
        *,
        run_id: str,
        tree_runs: Sequence[str],
        anchor: StepPath | None,
    ) -> StepPath:
        placeholders = ", ".join("?" for _ in tree_runs)
        rows = self._conn.execute(
            f"""
            SELECT rowid, * FROM steps
            WHERE run IN ({placeholders}) AND ejected_run IS NULL
            ORDER BY rowid ASC
            """,
            tuple(tree_runs),
        ).fetchall()
        if anchor is not None:
            match = next(
                (
                    row
                    for row in rows
                    if str(row["run"]) == anchor.run
                    and str(row["path"]) == anchor.local
                ),
                None,
            )
            if match is None:
                raise ValueError(
                    f"retry anchor is not visible in run {run_id}: {anchor}"
                )
            return anchor
        retryable = tuple(
            row
            for row in rows
            if str(row["status"]) in {"running", "failed", "canceled"}
        )
        candidate = next(
            (row for row in reversed(retryable) if str(row["kind"]) != "system"),
            retryable[-1] if retryable else None,
        )
        if candidate is None:
            raise ValueError(f"run has no retryable step: {run_id}")
        return StepPath.from_local(str(candidate["run"]), str(candidate["path"]))

    def _retry_step_suffix(
        self,
        *,
        tree_runs: Sequence[str],
        anchor: StepPath,
    ) -> tuple[StepPath, ...]:
        """Return the effective step suffix invalidated by one retry."""

        placeholders = ", ".join("?" for _ in tree_runs)
        rows = self._conn.execute(
            f"""
            SELECT rowid, * FROM steps
            WHERE run IN ({placeholders}) AND ejected_run IS NULL
            ORDER BY rowid ASC
            """,
            tuple(tree_runs),
        ).fetchall()
        keyed = {
            StepPath.from_local(str(row["run"]), str(row["path"])): row for row in rows
        }
        anchor_row = keyed.get(anchor)
        if anchor_row is None:
            raise ValueError(f"retry anchor step not found: {anchor}")
        cutoff = int(anchor_row["rowid"])
        run_rows = self._conn.execute(
            f"SELECT * FROM runs WHERE id IN ({placeholders})",
            tuple(tree_runs),
        ).fetchall()
        parents = {
            run.id: run.parent
            for run in (_run_from_row(row) for row in run_rows)
            if run.parent is not None
        }
        run_records = tuple(_run_from_row(row) for row in run_rows)
        root = next((run for run in run_records if run.parent is None), None)
        if root is not None and root.runnable_kind == "agic" and rows:
            cutoff = int(rows[0]["rowid"])
        current: StepPath | None = anchor
        while current is not None:
            for size in range(1, len(current.indices) + 1):
                ancestor = StepPath(current.run, current.indices[:size])
                row = keyed.get(ancestor)
                if row is not None and str(row["status"]) != "finished":
                    cutoff = min(cutoff, int(row["rowid"]))
            current = parents.get(current.run)
        return tuple(
            StepPath.from_local(str(row["run"]), str(row["path"]))
            for row in rows
            if int(row["rowid"]) >= cutoff
        )

    def begin_step(
        self,
        *,
        path: StepPath,
        kind: StepKind,
        input: Sequence[StepInput],
        given: Mapping[str, Any],
        started_at: str,
    ) -> StepRecord:
        """Project one step_begin event."""

        with self.write_transaction():
            self._conn.execute(
                """
                INSERT INTO steps(
                    run, path, kind, input, output, given, noted,
                    status, error, created_at, started_at, finished_at
                ) VALUES (?, ?, ?, ?, '[]', ?, '{}', 'running', NULL, ?, ?, NULL)
                ON CONFLICT(run, path) DO NOTHING
                """,
                (
                    path.run,
                    path.local,
                    kind,
                    _dump_json(step_inputs_to_data(tuple(input))),
                    _dump_json(dict(given)),
                    started_at,
                    started_at,
                ),
            )
            row = self._conn.execute(
                "SELECT * FROM steps WHERE run = ? AND path = ?",
                (path.run, path.local),
            ).fetchone()
        if row is None:
            raise RuntimeError(f"step begin projection failed: {path}")
        step = _step_from_row(row)
        if (
            step.kind != kind
            or step.input != tuple(input)
            or step.given != dict(given)
            or step.started_at != started_at
        ):
            raise ValueError(f"conflicting step_begin event: {path}")
        return step

    def finish_step(
        self,
        *,
        path: StepPath,
        kind: StepKind,
        status: StepStatus,
        output: Sequence[MessagePart],
        noted: Mapping[str, Any],
        error: str | None,
        finished_at: str,
    ) -> StepRecord:
        """Project one step_end event."""

        with self.write_transaction():
            existing = self._conn.execute(
                "SELECT * FROM steps WHERE run = ? AND path = ?",
                (path.run, path.local),
            ).fetchone()
            if existing is None:
                raise ValueError(f"step not found: {path}")
            existing_step = _step_from_row(existing)
            if existing_step.kind != kind:
                raise ValueError(f"step kind changed: {path}")
            if existing_step.status == "running":
                self._conn.execute(
                    """
                    UPDATE steps
                    SET output = ?, noted = ?, status = ?, error = ?, finished_at = ?
                    WHERE run = ? AND path = ?
                    """,
                    (
                        _dump_json(parts_to_data(output)),
                        _dump_json(dict(noted)),
                        status,
                        error,
                        finished_at,
                        path.run,
                        path.local,
                    ),
                )
            row = self._conn.execute(
                "SELECT * FROM steps WHERE run = ? AND path = ?",
                (path.run, path.local),
            ).fetchone()
        if row is None:
            raise RuntimeError(f"step end projection failed: {path}")
        step = _step_from_row(row)
        if (
            step.status != status
            or step.output != tuple(output)
            or step.noted != dict(noted)
            or step.error != error
            or step.finished_at != finished_at
        ):
            raise ValueError(f"conflicting step_end event: {path}")
        return step

    def list_steps(
        self, *, run_id: str, include_ejected: bool = False
    ) -> list[StepRecord]:
        clauses = ["run = ?"]
        if not include_ejected:
            clauses.append("ejected_run IS NULL")
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM steps WHERE {' AND '.join(clauses)}",
                (run_id,),
            ).fetchall()
        return sorted(
            (_step_from_row(row) for row in rows),
            key=lambda step: step.path.indices,
        )

    def list_steps_for_runs(
        self, *, run_ids: Sequence[str], include_ejected: bool = False
    ) -> dict[str, list[StepRecord]]:
        run_id_list = [item for item in run_ids if item]
        if not run_id_list:
            return {}
        placeholders = ", ".join("?" for _ in run_id_list)
        visible = "" if include_ejected else "AND ejected_run IS NULL"
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT * FROM steps
                WHERE run IN ({placeholders})
                {visible}
                """,
                tuple(run_id_list),
            ).fetchall()
        grouped: dict[str, list[StepRecord]] = {run_id: [] for run_id in run_id_list}
        for row in rows:
            record = _step_from_row(row)
            grouped.setdefault(record.run_id, []).append(record)
        for records in grouped.values():
            records.sort(key=lambda step: step.path.indices)
        return grouped

    def get_model_text(self, *, text_hash: str) -> str | None:
        """Return normalized model-request text by hash."""

        return self._get_model_texts({text_hash}).get(text_hash)

    def capture_model_call(
        self,
        *,
        target: Mapping[str, Any],
        call: ModelCall,
    ) -> dict[str, Any]:
        """Persist deduplicated normalized model-call inputs."""

        with self.write_transaction():
            instruction_ref = self._put_model_text(call.instructions)
            message_refs = [
                self._put_model_message(message) for message in call.messages
            ]
            toolset_ref = self._put_toolset(call.tools) if call.tools else None
        return {
            "model": _model_target_snapshot(target),
            "call": {
                "instructions": instruction_ref,
                "messages": message_refs,
                "tools": toolset_ref,
                "state": dict(call.state) if call.state is not None else None,
            },
        }

    def rebuild_model_call(self, step: StepRecord) -> ModelCall:
        """Rebuild the normalized model call captured by one model step."""

        return self.rebuild_model_calls((step,))[step.path]

    def rebuild_model_calls(
        self, steps: Sequence[StepRecord]
    ) -> dict[StepPath, ModelCall]:
        """Rebuild normalized model calls for several model steps in batches."""

        references: dict[
            StepPath,
            tuple[str, tuple[str, ...], str | None, dict[str, Any] | None],
        ] = {}
        instruction_hashes: set[str] = set()
        message_hashes: set[str] = set()
        toolset_hashes: set[str] = set()
        for step in steps:
            if step.kind != "model":
                raise ValueError(f"step is not a model call: {step.path}")
            raw_call = step.given.get("call")
            if not isinstance(raw_call, Mapping):
                raise ValueError(f"model call metadata is missing: {step.path}")
            instruction_ref = _required_text(
                raw_call.get("instructions"), "instructions"
            )
            raw_messages = raw_call.get("messages")
            if not isinstance(raw_messages, Sequence) or isinstance(
                raw_messages, (str, bytes, bytearray)
            ):
                raise ValueError(f"model message references are invalid: {step.path}")
            message_refs = tuple(
                _required_text(message_ref, f"messages[{index}]")
                for index, message_ref in enumerate(raw_messages)
            )
            raw_toolset_ref = raw_call.get("tools")
            toolset_ref = (
                _required_text(raw_toolset_ref, "tools")
                if raw_toolset_ref is not None
                else None
            )
            raw_state = raw_call.get("state")
            if raw_state is not None and not isinstance(raw_state, Mapping):
                raise ValueError(f"model adapter state is invalid: {step.path}")
            state = dict(raw_state) if isinstance(raw_state, Mapping) else None
            references[step.path] = (
                instruction_ref,
                message_refs,
                toolset_ref,
                state,
            )
            instruction_hashes.add(instruction_ref)
            message_hashes.update(message_refs)
            if toolset_ref is not None:
                toolset_hashes.add(toolset_ref)

        texts = self._get_model_texts(instruction_hashes)
        messages = self._get_model_messages(message_hashes)
        toolsets = self._get_toolsets(toolset_hashes)
        calls: dict[StepPath, ModelCall] = {}
        for path, (
            instruction_ref,
            message_refs,
            toolset_ref,
            state,
        ) in references.items():
            instructions = texts.get(instruction_ref)
            if instructions is None:
                raise ValueError(f"model instructions are missing: {instruction_ref}")
            missing_message = next(
                (item for item in message_refs if item not in messages), None
            )
            if missing_message is not None:
                raise ValueError(f"model message is missing: {missing_message}")
            if toolset_ref is not None and toolset_ref not in toolsets:
                raise ValueError(f"model toolset is missing: {toolset_ref}")
            calls[path] = ModelCall(
                instructions=instructions,
                messages=[messages[item] for item in message_refs],
                tools=toolsets[toolset_ref] if toolset_ref is not None else (),
                state=state,
            )
        return calls

    def _get_model_texts(self, text_hashes: set[str]) -> dict[str, str]:
        rows = self._content_rows(
            table="model_texts",
            value_column="body",
            hashes=text_hashes,
        )
        texts: dict[str, str] = {}
        for text_hash, raw in rows.items():
            body = str(raw)
            _verify_content_hash(body, expected=text_hash, label="model text")
            texts[text_hash] = body
        return texts

    def _get_model_messages(self, message_hashes: set[str]) -> dict[str, Message]:
        rows = self._content_rows(
            table="model_messages",
            value_column="data",
            hashes=message_hashes,
        )
        return {
            message_hash: _model_message_from_stored(message_hash, str(raw))
            for message_hash, raw in rows.items()
        }

    def _get_toolsets(
        self, toolset_hashes: set[str]
    ) -> dict[str, tuple[ToolDefinition, ...]]:
        rows = self._content_rows(
            table="model_toolsets",
            value_column="data",
            hashes=toolset_hashes,
        )
        return {
            toolset_hash: _toolset_from_stored(toolset_hash, str(raw))
            for toolset_hash, raw in rows.items()
        }

    def _content_rows(
        self,
        *,
        table: str,
        value_column: str,
        hashes: set[str],
    ) -> dict[str, object]:
        if not hashes:
            return {}
        values = tuple(sorted(hashes))
        rows: list[sqlite3.Row] = []
        with self._lock:
            for offset in range(0, len(values), 500):
                chunk = values[offset : offset + 500]
                placeholders = ", ".join("?" for _ in chunk)
                rows.extend(
                    self._conn.execute(
                        f"SELECT hash, {value_column} FROM {table} "
                        f"WHERE hash IN ({placeholders})",
                        chunk,
                    ).fetchall()
                )
        return {str(row["hash"]): row[value_column] for row in rows}

    def _put_model_text(self, body: str) -> str:
        text_hash = _content_hash(body)
        self._conn.execute(
            "INSERT OR IGNORE INTO model_texts(hash, body) VALUES (?, ?)",
            (text_hash, body),
        )
        return text_hash

    def _put_model_message(self, message: Message) -> str:
        data = _dump_json(message.to_data())
        message_hash = _content_hash(data)
        self._conn.execute(
            "INSERT OR IGNORE INTO model_messages(hash, data) VALUES (?, ?)",
            (message_hash, data),
        )
        return message_hash

    def _put_toolset(self, tools: Sequence[ToolDefinition]) -> str:
        data = _dump_json([tool.to_data() for tool in tools])
        toolset_hash = _content_hash(data)
        self._conn.execute(
            "INSERT OR IGNORE INTO model_toolsets(hash, data) VALUES (?, ?)",
            (toolset_hash, data),
        )
        return toolset_hash

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
        steps_by_run = self.list_steps_for_runs(run_ids=tuple(run.id for run in runs))
        results: list[Message] = []
        for run in runs:
            inputs = self.list_run_controls(run_id=run.id)
            if inputs:
                results.extend(item.input for item in inputs if item.input is not None)
            for step in steps_by_run.get(run.id, ()):
                results.extend(_replay_messages_from_step(step))
        return _recent_valid_model_history(results, limit=limit)

    def _conversation_runs(self, *, thread_id: str, limit: int) -> list[RunRecord]:
        current = list(
            self.list_thread_history_chronological(thread_id=thread_id, limit=None)
        )
        return current[-limit:]

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

    def list_run_controls_for_runs(
        self,
        *,
        run_ids: Sequence[str],
        kind: RunControlKind | None = None,
    ) -> dict[str, tuple[RunControlRecord, ...]]:
        """Return controls for several runs, grouped by run id."""

        selected = tuple(dict.fromkeys(item for item in run_ids if item))
        if not selected:
            return {}
        grouped: dict[str, list[RunControlRecord]] = {run_id: [] for run_id in selected}
        with self._lock:
            for offset in range(0, len(selected), 500):
                chunk = selected[offset : offset + 500]
                placeholders = ", ".join("?" for _ in chunk)
                params: tuple[object, ...] = chunk
                kind_clause = ""
                if kind is not None:
                    kind_clause = " AND kind = ?"
                    params = (*chunk, kind)
                rows = self._conn.execute(
                    f"""
                    SELECT * FROM run_controls
                    WHERE run IN ({placeholders}){kind_clause}
                    ORDER BY run ASC, "index" ASC
                    """,
                    params,
                ).fetchall()
                for row in rows:
                    control = _run_control_from_row(row)
                    grouped[control.run].append(control)
        return {run_id: tuple(controls) for run_id, controls in grouped.items()}

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

    def latest_run_control_revision(self) -> int:
        """Return the latest durable run-control change revision."""

        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(revision), 0) AS sequence FROM run_controls"
            ).fetchone()
        return int(row["sequence"]) if row is not None else 0

    def changed_run_controls(
        self,
        *,
        after_revision: int,
    ) -> tuple[int, tuple[RunControlRecord, ...]]:
        """Return controls changed after one process-local polling cursor."""

        if after_revision < 0:
            raise ValueError("run control cursor must not be negative")
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM run_controls
                WHERE revision > ?
                ORDER BY revision ASC, run ASC, "index" ASC
                """,
                (after_revision,),
            ).fetchall()
        if not rows:
            return after_revision, ()
        return (
            max(int(row["revision"]) for row in rows),
            tuple(_run_control_from_row(row) for row in rows),
        )

    def _next_run_control_revision(self) -> int:
        row = self._conn.execute(
            "SELECT COALESCE(MAX(revision), 0) + 1 AS revision FROM run_controls"
        ).fetchone()
        return int(row["revision"]) if row is not None else 1

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.execute("PRAGMA busy_timeout=30000;")
            version = int(self._conn.execute("PRAGMA user_version").fetchone()[0])
            allowed = (
                (_SCHEMA_VERSION,)
                if self.read_only
                else (0, *_MIGRATABLE_SCHEMA_VERSIONS)
            )
            if version not in allowed:
                raise RunStoreSchemaError(
                    version,
                    current=_SCHEMA_VERSION,
                    supported=_MIGRATABLE_SCHEMA_VERSIONS,
                    read_only=self.read_only,
                )
            if self.read_only:
                self._conn.execute("PRAGMA query_only=ON;")
                return
            self._conn.execute("PRAGMA journal_mode=WAL;")
            self._conn.execute("PRAGMA synchronous=NORMAL;")
            self._conn.execute("PRAGMA foreign_keys=ON;")
            self._conn.execute("BEGIN IMMEDIATE")
            version = int(self._conn.execute("PRAGMA user_version").fetchone()[0])
            if version not in (0, *_MIGRATABLE_SCHEMA_VERSIONS):
                self._conn.rollback()
                raise RunStoreSchemaError(
                    version,
                    current=_SCHEMA_VERSION,
                    supported=_MIGRATABLE_SCHEMA_VERSIONS,
                    read_only=False,
                )
            if version == 13:
                self._conn.execute("DROP INDEX IF EXISTS idx_run_controls_request")
                try:
                    self._conn.execute(
                        """
                        CREATE UNIQUE INDEX idx_run_controls_request
                        ON run_controls(request_id)
                        WHERE request_id IS NOT NULL
                        """
                    )
                except sqlite3.IntegrityError as exc:
                    self._conn.rollback()
                    raise RuntimeError(
                        "run store schema migration found duplicate request ids"
                    ) from exc
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
                    ejected_thread TEXT,
                    ejected_run TEXT,
                    ejected_index INTEGER,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT
                )
                """
            )
            run_columns = {
                str(row["name"])
                for row in self._conn.execute("PRAGMA table_info(runs)").fetchall()
            }
            if "superseded_by_thread" in run_columns:
                self._conn.execute(
                    "ALTER TABLE runs RENAME COLUMN superseded_by_thread TO ejected_thread"
                )
                run_columns.remove("superseded_by_thread")
                run_columns.add("ejected_thread")
            if "superseded_by_index" in run_columns:
                self._conn.execute(
                    "ALTER TABLE runs RENAME COLUMN superseded_by_index TO ejected_index"
                )
                run_columns.remove("superseded_by_index")
                run_columns.add("ejected_index")
            if "ejected_thread" not in run_columns:
                self._conn.execute("ALTER TABLE runs ADD COLUMN ejected_thread TEXT")
            if "ejected_run" not in run_columns:
                self._conn.execute("ALTER TABLE runs ADD COLUMN ejected_run TEXT")
            if "ejected_index" not in run_columns:
                self._conn.execute("ALTER TABLE runs ADD COLUMN ejected_index INTEGER")
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS run_controls (
                    run TEXT NOT NULL,
                    "index" INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    timing TEXT NOT NULL,
                    input TEXT,
                    source TEXT,
                    anchor TEXT,
                    request_id TEXT,
                    context TEXT NOT NULL,
                    status TEXT NOT NULL,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    finished_at TEXT,
                    claimed INTEGER NOT NULL DEFAULT 0,
                    revision INTEGER NOT NULL,
                    PRIMARY KEY(run, "index"),
                    FOREIGN KEY(run) REFERENCES runs(id)
                )
                """
            )
            run_control_columns = {
                str(row["name"])
                for row in self._conn.execute(
                    "PRAGMA table_info(run_controls)"
                ).fetchall()
            }
            if "revision" not in run_control_columns:
                self._conn.execute(
                    """
                    ALTER TABLE run_controls
                    ADD COLUMN revision INTEGER NOT NULL DEFAULT 0
                    """
                )
            if "claimed" not in run_control_columns:
                self._conn.execute(
                    """
                    ALTER TABLE run_controls
                    ADD COLUMN claimed INTEGER NOT NULL DEFAULT 0
                    """
                )
            if "source_run" in run_control_columns:
                self._conn.execute(
                    "ALTER TABLE run_controls RENAME COLUMN source_run TO source"
                )
                run_control_columns.remove("source_run")
                run_control_columns.add("source")
            if "anchor_step" in run_control_columns:
                self._conn.execute(
                    "ALTER TABLE run_controls RENAME COLUMN anchor_step TO anchor"
                )
                run_control_columns.remove("anchor_step")
                run_control_columns.add("anchor")
            if "source" not in run_control_columns:
                self._conn.execute("ALTER TABLE run_controls ADD COLUMN source TEXT")
            if "anchor" not in run_control_columns:
                self._conn.execute("ALTER TABLE run_controls ADD COLUMN anchor TEXT")
            self._conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_run_controls_request
                ON run_controls(request_id)
                WHERE request_id IS NOT NULL
                """
            )
            self._conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_run_controls_revision
                ON run_controls(revision)
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS thread_controls (
                    thread TEXT NOT NULL,
                    "index" INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    source TEXT,
                    anchor TEXT,
                    request_id TEXT,
                    expected_head_thread TEXT,
                    expected_head_index INTEGER,
                    context TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    finished_at TEXT,
                    PRIMARY KEY(thread, "index")
                )
                """
            )
            thread_control_columns = {
                str(row["name"])
                for row in self._conn.execute(
                    "PRAGMA table_info(thread_controls)"
                ).fetchall()
            }
            if "source_thread" in thread_control_columns:
                self._conn.execute(
                    "ALTER TABLE thread_controls RENAME COLUMN source_thread TO source"
                )
                thread_control_columns.remove("source_thread")
                thread_control_columns.add("source")
            if "anchor_run" in thread_control_columns:
                self._conn.execute(
                    "ALTER TABLE thread_controls RENAME COLUMN anchor_run TO anchor"
                )
                thread_control_columns.remove("anchor_run")
                thread_control_columns.add("anchor")
            if "error" in thread_control_columns:
                self._conn.execute("ALTER TABLE thread_controls DROP COLUMN error")
            self._conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_thread_controls_request
                ON thread_controls(request_id)
                WHERE request_id IS NOT NULL
                """
            )
            step_table = self._conn.execute(
                """
                SELECT 1 FROM sqlite_master
                WHERE type = 'table' AND name = 'steps'
                """
            ).fetchone()
            if step_table is None:
                _create_steps_table(self._conn)
            else:
                step_columns = {
                    str(row["name"])
                    for row in self._conn.execute("PRAGMA table_info(steps)").fetchall()
                }
                if "run" not in step_columns or "path" not in step_columns:
                    given = "given" if "given" in step_columns else "context"
                    noted = "noted" if "noted" in step_columns else "detail"
                    self._conn.execute("ALTER TABLE steps RENAME TO legacy_steps")
                    _create_steps_table(self._conn)
                    self._conn.execute(
                        f"""
                        INSERT INTO steps(
                            run, path, kind, input, output, given, noted,
                            status, error, created_at, started_at, finished_at
                        )
                        SELECT
                            CASE instr(parent, '/')
                                WHEN 0 THEN parent
                                ELSE substr(parent, 1, instr(parent, '/') - 1)
                            END,
                            CASE instr(parent, '/')
                                WHEN 0 THEN CAST("index" AS TEXT)
                                ELSE substr(parent, instr(parent, '/') + 1)
                                     || '/' || "index"
                            END,
                            kind, input, output, {given}, {noted},
                            status, error, created_at, started_at, finished_at
                        FROM legacy_steps
                        """
                    )
                    self._conn.execute("DROP TABLE legacy_steps")
                else:
                    if "context" in step_columns and "given" not in step_columns:
                        self._conn.execute(
                            "ALTER TABLE steps RENAME COLUMN context TO given"
                        )
                    if "detail" in step_columns and "noted" not in step_columns:
                        self._conn.execute(
                            "ALTER TABLE steps RENAME COLUMN detail TO noted"
                        )
                step_columns = {
                    str(row["name"])
                    for row in self._conn.execute("PRAGMA table_info(steps)").fetchall()
                }
                if "ejected_run" not in step_columns:
                    self._conn.execute("ALTER TABLE steps ADD COLUMN ejected_run TEXT")
                if "ejected_index" not in step_columns:
                    self._conn.execute(
                        "ALTER TABLE steps ADD COLUMN ejected_index INTEGER"
                    )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS model_texts (
                    hash TEXT PRIMARY KEY,
                    body TEXT NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS model_messages (
                    hash TEXT PRIMARY KEY,
                    data TEXT NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS model_toolsets (
                    hash TEXT PRIMARY KEY,
                    data TEXT NOT NULL
                )
                """
            )
            for legacy_table in ("templates", "prompts"):
                legacy = self._conn.execute(
                    """
                    SELECT 1 FROM sqlite_master
                    WHERE type = 'table' AND name = ?
                    """,
                    (legacy_table,),
                ).fetchone()
                if legacy is None:
                    continue
                self._conn.execute(
                    f"""
                    INSERT OR IGNORE INTO model_texts(hash, body)
                    SELECT hash, body FROM {legacy_table}
                    """
                )
                self._conn.execute(f"DROP TABLE {legacy_table}")
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_runs_thread_created ON runs(thread, created_at)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_threads_updated ON threads(updated_at)"
            )
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_steps_run ON steps(run)")
            self._conn.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")
            self._conn.commit()


def _create_steps_table(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE steps (
            run TEXT NOT NULL,
            path TEXT NOT NULL,
            kind TEXT NOT NULL,
            input TEXT NOT NULL,
            output TEXT NOT NULL,
            given TEXT NOT NULL,
            noted TEXT NOT NULL,
            status TEXT NOT NULL,
            error TEXT,
            created_at TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            ejected_run TEXT,
            ejected_index INTEGER,
            PRIMARY KEY(run, path)
        )
        """
    )


def _dump_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _content_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _verify_content_hash(value: str, *, expected: str, label: str) -> None:
    if _content_hash(value) != expected:
        raise ValueError(f"{label} is corrupted: {expected}")


def _load_json(value: str) -> Any:
    return json.loads(value)


def _required_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _model_message_from_stored(message_hash: str, stored: str) -> Message:
    _verify_content_hash(
        stored,
        expected=message_hash,
        label="model message",
    )
    try:
        data = _load_json(stored)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"model message is invalid: {message_hash}") from exc
    if not isinstance(data, Mapping):
        raise ValueError(f"model message is invalid: {message_hash}")
    try:
        return Message.from_data(data)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"model message is invalid: {message_hash}") from exc


def _toolset_from_stored(toolset_hash: str, stored: str) -> tuple[ToolDefinition, ...]:
    _verify_content_hash(
        stored,
        expected=toolset_hash,
        label="model toolset",
    )
    try:
        data = _load_json(stored)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"model toolset is invalid: {toolset_hash}") from exc
    if not isinstance(data, Sequence) or isinstance(data, (str, bytes, bytearray)):
        raise ValueError(f"model toolset is invalid: {toolset_hash}")
    tools: list[ToolDefinition] = []
    for index, raw_tool in enumerate(data):
        if not isinstance(raw_tool, Mapping):
            raise ValueError(f"model toolset item is invalid: {toolset_hash}[{index}]")
        tool_data = cast(Mapping[str, Any], raw_tool)
        parameters = tool_data.get("parameters")
        if not isinstance(parameters, Mapping):
            raise ValueError(
                f"model tool parameters are invalid: {toolset_hash}[{index}]"
            )
        try:
            tools.append(ToolDefinition.from_data(tool_data))
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"model toolset item is invalid: {toolset_hash}[{index}]"
            ) from exc
    return tuple(tools)


def _model_target_snapshot(target: Mapping[str, Any]) -> dict[str, Any]:
    snapshot = {
        name: _required_text(target.get(name), f"model {name}")
        for name in ("ref", "provider", "name", "model", "adapter")
    }
    base_url = target.get("base_url")
    if base_url is not None and not isinstance(base_url, str):
        raise ValueError("model base_url must be text or null")
    scope = target.get("scope")
    if scope is not None and not isinstance(scope, str):
        raise ValueError("model scope must be text or null")
    tags = target.get("tags", ())
    if (
        not isinstance(tags, Sequence)
        or isinstance(tags, (str, bytes, bytearray))
        or not all(isinstance(tag, str) for tag in tags)
    ):
        raise ValueError("model tags must be a list of text")
    options = target.get("options", {})
    if not isinstance(options, Mapping):
        raise ValueError("model options must be an object")
    tools = target.get("tools", True)
    if not isinstance(tools, bool):
        raise ValueError("model tools must be boolean")
    streaming = target.get("streaming", True)
    if not isinstance(streaming, bool):
        raise ValueError("model streaming must be boolean")
    return {
        **snapshot,
        "base_url": base_url,
        "scope": scope,
        "tags": list(tags),
        "options": dict(options),
        "tools": tools,
        "streaming": streaming,
    }


def _validate_request_id(request_id: str | None) -> None:
    if request_id is not None and (
        not request_id.strip() or request_id != request_id.strip()
    ):
        raise ValueError(f"invalid request id: {request_id!r}")


def _history_tail(
    runs: Sequence[RunRecord], *, limit: int | None
) -> tuple[RunRecord, ...]:
    if limit is None:
        return tuple(runs)
    if limit < 0:
        raise ValueError("history limit must not be negative")
    if limit == 0:
        return ()
    return tuple(runs[-limit:])


def _run_from_row(row: sqlite3.Row) -> RunRecord:
    context_raw = _load_json(str(row["context"])) if row["context"] is not None else {}
    output_raw = _load_json(str(row["output"])) if row["output"] is not None else None
    return RunRecord(
        id=str(row["id"]),
        parent=(
            StepPath.parse(str(row["parent"])) if row["parent"] is not None else None
        ),
        thread=str(row["thread"]),
        input=RunInputRef.from_data(
            cast(Mapping[str, Any], _load_json(str(row["input"])))
        ),
        output=(
            value_ref_from_data(cast(Mapping[str, Any], output_raw))
            if isinstance(output_raw, Mapping)
            else None
        ),
        context=dict(context_raw) if isinstance(context_raw, Mapping) else {},
        status=cast(RunStatus, row["status"]),
        error=str(row["error"]) if row["error"] is not None else None,
        ejected=_run_ejection_from_row(row),
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
    given_raw = _load_json(str(raw["given"]))
    noted_raw = _load_json(str(raw["noted"]))
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
        path=StepPath.from_local(str(raw["run"]), str(raw["path"])),
        kind=cast(StepKind, raw["kind"]),
        input=step_inputs_from_data(
            [item for item in input_items if isinstance(item, Mapping)]
        ),
        output=parts_from_data(
            [item for item in output_items if isinstance(item, Mapping)]
        ),
        given=dict(given_raw) if isinstance(given_raw, Mapping) else {},
        noted=dict(noted_raw) if isinstance(noted_raw, Mapping) else {},
        status=cast(StepStatus, raw["status"]),
        error=str(raw["error"]) if raw["error"] is not None else None,
        ejected=(
            RunControlRef(
                run=str(raw["ejected_run"]),
                index=int(cast(int | str, raw["ejected_index"])),
            )
            if raw.get("ejected_run") is not None
            and raw.get("ejected_index") is not None
            else None
        ),
        created_at=str(raw["created_at"]),
        started_at=str(raw["started_at"]),
        finished_at=str(raw["finished_at"]) if raw["finished_at"] is not None else None,
    )


def _run_control_from_row(row: sqlite3.Row) -> RunControlRecord:
    input_raw = _load_json(str(row["input"])) if row["input"] is not None else None
    context_raw = _load_json(str(row["context"])) if row["context"] is not None else {}
    return RunControlRecord(
        run=str(row["run"]),
        index=int(row["index"]),
        kind=cast(RunControlKind, row["kind"]),
        timing=cast(ControlTiming, row["timing"]),
        input=Message.from_data(input_raw) if isinstance(input_raw, Mapping) else None,
        source=str(row["source"]) if row["source"] is not None else None,
        anchor=(
            StepPath.parse(str(row["anchor"])) if row["anchor"] is not None else None
        ),
        request_id=str(row["request_id"]) if row["request_id"] is not None else None,
        context=dict(context_raw) if isinstance(context_raw, Mapping) else {},
        status=row["status"],
        error=str(row["error"]) if row["error"] is not None else None,
        created_at=str(row["created_at"]),
        finished_at=str(row["finished_at"]) if row["finished_at"] is not None else None,
    )


def _thread_control_from_row(row: sqlite3.Row) -> ThreadControlRecord:
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
        source=str(row["source"]) if row["source"] is not None else None,
        anchor=str(row["anchor"]) if row["anchor"] is not None else None,
        request_id=str(row["request_id"]) if row["request_id"] is not None else None,
        expected_head=expected_head,
        context=dict(context_raw) if isinstance(context_raw, Mapping) else {},
        status=row["status"],
        created_at=str(row["created_at"]),
        finished_at=str(row["finished_at"]) if row["finished_at"] is not None else None,
    )


def _run_ejection_from_row(
    row: sqlite3.Row,
) -> ThreadControlRef | RunControlRef | None:
    if row["ejected_index"] is None:
        return None
    if row["ejected_thread"] is not None:
        return ThreadControlRef(
            thread=str(row["ejected_thread"]),
            index=int(row["ejected_index"]),
        )
    if row["ejected_run"] is not None:
        return RunControlRef(
            run=str(row["ejected_run"]),
            index=int(row["ejected_index"]),
        )
    return None


def _replay_messages_from_step(step: StepRecord) -> list[Message]:
    role = step_message_role(step.kind)
    if role is None or not step.output:
        return []
    return [Message(role=role, parts=tuple(step.output))]


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
        if any(not tool_call_id for tool_call_id in tool_call_ids) or len(
            set(tool_call_ids)
        ) != len(tool_call_ids):
            index += 1
            continue
        tool_group: list[Message] = []
        remaining = set(tool_call_ids)
        valid = True
        cursor = index + 1
        while cursor < len(messages) and messages[cursor].role == "tool":
            tool_message = messages[cursor]
            result_ids = _message_tool_result_ids(tool_message)
            matched = set(result_ids)
            if (
                not result_ids
                or any(not result_id for result_id in result_ids)
                or len(matched) != len(result_ids)
                or not matched.issubset(remaining)
            ):
                valid = False
            else:
                tool_group.append(tool_message)
                remaining -= matched
            cursor += 1
        if valid and not remaining:
            groups.append((message, *tool_group))
        index = cursor
    return groups


def _message_tool_call_ids(message: Message) -> tuple[str, ...]:
    return tuple(
        part.tool_call_id for part in message.parts if isinstance(part, ToolCallPart)
    )


def _message_tool_result_ids(message: Message) -> tuple[str, ...]:
    return tuple(
        part.tool_call_id for part in message.parts if isinstance(part, ToolResultPart)
    )
