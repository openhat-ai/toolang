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
    Part,
    ToolCallPart,
    ToolResultPart,
    message_text,
)
from toolang.base.types.run import ModelCall
from toolang.lang.types import Array, Struct, Value
from toolang.base.types.tool import ToolDefinition
from toolang.base.types.policy import RunLimits
from toolang.common.time import utc_now
from .errors import RunStoreSchemaError
from .records import (
    CreateControlPayload,
    ControlPayload,
    ForkControlPayload,
    PreparationControlPayload,
    RerunControlPayload,
    RunScopedControlPayload,
    RetryControlPayload,
    RewindControlPayload,
    RunControlPayload,
    SteerControlPayload,
    CancelControlPayload,
    control_payload_from_data,
    control_payload_to_data,
    RunControlRecord,
    RunRecord,
    StepRecord,
    StoredModelStepGiven,
    StoredStepGiven,
    ThreadPeer,
    ThreadControlPayload,
    ThreadControlRecord,
    ThreadRecord,
    execution_error_from_data,
    execution_error_to_data,
    step_message_role,
    local_from_data,
    local_to_data,
    occurrence_from_data,
    occurrence_to_data,
    pointers_from_data,
    pointers_to_data,
    step_noted_from_data,
    step_noted_to_data,
    stored_step_given_from_data,
    stored_step_given_to_data,
)
from .types import (
    ControlKind,
    ControlRef,
    ControlStatus,
    AgentResources,
    ExecutionError,
    ControlTiming,
    RunStatus,
    StepKind,
    StepGiven,
    StepNoted,
    StepStatus,
    StepPath,
    Local,
    ModelStepGiven,
    Occurrence,
    Pointer,
    TypedPointer,
    validate_runtime_value,
    validate_execution_id,
)
from .values import parts_from_local

_SCHEMA_VERSION = 30
_MIGRATABLE_SCHEMA_VERSIONS = (28, 29, _SCHEMA_VERSION)


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

    def _insert_control(
        self,
        *,
        scope: Literal["run", "thread"],
        target: str,
        index: int,
        kind: ControlKind,
        timing: ControlTiming,
        payload: ControlPayload,
        request: str | None,
        status: ControlStatus,
        error: str | None,
        created_at: str,
        finished_at: str | None,
        claimed: bool,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO controls(
                scope, target, "index", kind, request, status, error, timing,
                payload, created_at, finished_at, _claimed, _revision
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                scope,
                target,
                index,
                kind,
                request,
                status,
                error,
                timing,
                _dump_json(control_payload_to_data(payload)),
                created_at,
                finished_at,
                int(claimed),
                self._next_run_control_revision(),
            ),
        )

    def accept_run(
        self,
        *,
        run_id: str,
        parent: StepPath | None,
        thread: str,
        resources: AgentResources,
        limits: RunLimits,
        state: str,
        runnable: str,
        model: str,
        locals: tuple[Local, ...],
        sandbox: str | None,
        occurrence: Occurrence | None,
        request_id: str | None,
        created_at: str,
        kind: Literal["run", "rerun"] = "run",
        source: str | None = None,
    ) -> tuple[RunRecord, RunControlRecord]:
        """Atomically insert one new run and its entry control."""

        validate_execution_id(run_id, label="run id")
        validate_execution_id(thread, label="thread id")
        if parent is None:
            _validate_canonical_sandbox(sandbox)
        if kind == "run" and source is not None:
            raise ValueError("run control cannot have a source run")
        if kind == "rerun" and source is None:
            raise ValueError("rerun control requires a source run")
        if source is not None:
            validate_execution_id(source, label="source run id")
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
                        "SELECT 1 FROM controls WHERE request = ?",
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
                if parent is not None:
                    parent_row = self._conn.execute(
                        """
                        SELECT runs.thread
                        FROM steps
                        JOIN runs ON runs.id = steps.run
                        WHERE steps.run = ? AND steps.path = ?
                        """,
                        (parent.run, parent.local),
                    ).fetchone()
                    if parent_row is None:
                        raise ValueError(f"parent step not found: {parent}")
                    if str(parent_row["thread"]) != thread:
                        raise ValueError("child run must share its parent's thread")
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
                    if source_record.status not in {"succeeded", "failed", "canceled"}:
                        raise ValueError(f"rerun source is not terminal: {source}")
                self._conn.execute(
                    """
                    INSERT INTO runs(
                        id, parent, thread, control_target, control_index,
                        output, occurrence, status, error,
                        ejected_by_target, ejected_by_index,
                        created_at, started_at, finished_at
                    ) VALUES (
                        ?, ?, ?, ?, 0,
                        NULL, ?, 'pending', NULL,
                        NULL, NULL, ?, NULL, NULL
                    )
                    """,
                    (
                        run_id,
                        str(parent) if parent is not None else None,
                        thread,
                        run_id,
                        _dump_json(occurrence_to_data(occurrence))
                        if occurrence is not None
                        else None,
                        created_at,
                    ),
                )
                payload = (
                    RunControlPayload(
                        resources=resources,
                        limits=limits,
                        state=state,
                        runnable=runnable,
                        model=model,
                        locals=locals,
                        sandbox=sandbox,
                    )
                    if kind == "run"
                    else RerunControlPayload(
                        resources=resources,
                        limits=limits,
                        state=state,
                        runnable=runnable,
                        model=model,
                        locals=locals,
                        rerun_from=cast(str, source),
                        sandbox=sandbox,
                    )
                )
                self._insert_control(
                    scope="run",
                    target=run_id,
                    index=0,
                    kind=kind,
                    timing="immediate",
                    payload=payload,
                    request=request_id,
                    status="pending",
                    error=None,
                    created_at=created_at,
                    finished_at=None,
                    claimed=False,
                )
                run_row = self._conn.execute(
                    "SELECT * FROM runs WHERE id = ?", (run_id,)
                ).fetchone()
                control_row = self._conn.execute(
                    'SELECT * FROM controls WHERE target = ? AND "index" = 0',
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
        kind: ControlKind,
        timing: ControlTiming,
        locals: tuple[Local, ...],
        request_id: str | None,
        created_at: str,
    ) -> RunControlRecord:
        """Atomically allocate and accept one steer or cancel control."""

        validate_execution_id(run_id, label="run id")
        if kind not in {"steer", "cancel"}:
            raise ValueError(f"unsupported run control kind: {kind}")
        if timing not in {"immediate", "next_step", "next_call"}:
            raise ValueError(f"unsupported run control timing: {timing}")
        if kind == "steer":
            if len(locals) != 1:
                raise ValueError("steer control requires one primary local")
            primary = locals[0]
            if (
                primary.name != "_"
                or primary.type != "Part[]"
                or primary.dim != 0
                or isinstance(primary.value, TypedPointer)
                or not isinstance(primary.value, Array)
                or not all(isinstance(item, Part) for item in primary.value)
            ):
                raise ValueError("steer control requires a concrete primary Part[]")
        elif len(locals) > 1 or (
            locals
            and (
                locals[0].name != "_"
                or locals[0].type != "Text"
                or locals[0].dim != 0
                or not isinstance(locals[0].value, str)
            )
        ):
            raise ValueError("cancel control accepts only one primary Text local")
        _validate_request_id(request_id)

        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                if request_id is not None and (
                    self._conn.execute(
                        "SELECT 1 FROM controls WHERE request = ?",
                        (request_id,),
                    ).fetchone()
                    is not None
                ):
                    raise ValueError(
                        f"run control request already exists: {request_id}"
                    )
                run = self._conn.execute(
                    """
                    SELECT status, control_target, control_index
                    FROM runs
                    WHERE id = ?
                    """,
                    (run_id,),
                ).fetchone()
                if run is None:
                    raise ValueError(f"run not found: {run_id}")
                if str(run["status"]) not in {"pending", "running"}:
                    raise ValueError(f"run is not active: {run_id}")
                if kind == "steer":
                    preparation_row = self._conn.execute(
                        "SELECT kind, payload FROM controls "
                        'WHERE target = ? AND "index" = ?',
                        (str(run["control_target"]), int(run["control_index"])),
                    ).fetchone()
                    if preparation_row is None:
                        raise ValueError(f"run control is missing: {run_id}")
                    preparation = control_payload_from_data(
                        cast(ControlKind, preparation_row["kind"]),
                        _load_json(str(preparation_row["payload"])),
                    )
                    if not isinstance(preparation, PreparationControlPayload) or not (
                        preparation.runnable.startswith("agic:")
                    ):
                        raise ValueError("steer controls require an agic run")
                row = self._conn.execute(
                    'SELECT COALESCE(MAX("index"), -1) + 1 AS next_index '
                    "FROM controls WHERE target = ?",
                    (run_id,),
                ).fetchone()
                index = int(row["next_index"]) if row is not None else 0
                payload = (
                    SteerControlPayload(locals)
                    if kind == "steer"
                    else CancelControlPayload(locals)
                )
                self._insert_control(
                    scope="run",
                    target=run_id,
                    index=index,
                    kind=kind,
                    timing=timing,
                    payload=payload,
                    request=request_id,
                    status="pending",
                    error=None,
                    created_at=created_at,
                    finished_at=None,
                    claimed=False,
                )
                inserted = self._conn.execute(
                    'SELECT * FROM controls WHERE target = ? AND "index" = ?',
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
        resources: AgentResources,
        limits: RunLimits,
        state: str,
        runnable: str,
        model: str,
        locals: tuple[Local, ...] | None,
        sandbox: str,
        request_id: str | None,
        created_at: str,
    ) -> tuple[RunRecord, RunControlRecord, tuple[StepPath, ...]]:
        """Atomically cut one root run at a step and reopen it for execution."""

        validate_execution_id(run_id, label="run id")
        _validate_canonical_sandbox(sandbox)
        _validate_request_id(request_id)
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                if request_id is not None and (
                    self._conn.execute(
                        "SELECT 1 FROM controls WHERE request = ?",
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
                if run.status not in {"succeeded", "failed", "canceled"}:
                    raise ValueError(f"run is not terminal: {run_id}")
                preparation_row = self._conn.execute(
                    'SELECT * FROM controls WHERE target = ? AND "index" = ?',
                    (run.control.target, run.control.index),
                ).fetchone()
                preparation = (
                    _run_control_from_row(preparation_row)
                    if preparation_row is not None
                    else None
                )
                preparation_payload = (
                    preparation.payload if preparation is not None else None
                )
                if not isinstance(
                    preparation_payload,
                    RunControlPayload | RerunControlPayload | RetryControlPayload,
                ):
                    raise ValueError(f"run preparation not found: {run_id}")
                if preparation_payload.state != state:
                    raise ValueError(
                        f"retry state no longer matches original run: {run_id}; use rerun"
                    )
                if preparation_payload.sandbox is None:
                    raise ValueError(
                        f"retry sandbox is unknown for run {run_id}; use rerun"
                    )
                if preparation_payload.sandbox != sandbox:
                    raise ValueError(
                        f"retry sandbox {sandbox} does not match original sandbox "
                        f"{preparation_payload.sandbox} for run {run_id}; use rerun"
                    )
                tree_runs = self._root_tree_runs(run_id)
                resolved_anchor = self._resolve_retry_anchor(
                    run_id=run_id,
                    tree_runs=tree_runs,
                    anchor=anchor,
                    run_status=run.status,
                )
                index_row = self._conn.execute(
                    'SELECT COALESCE(MAX("index"), -1) + 1 AS next_index '
                    "FROM controls WHERE target = ?",
                    (run_id,),
                ).fetchone()
                index = int(index_row["next_index"]) if index_row is not None else 1
                trimmed = (
                    self._retry_step_suffix(
                        tree_runs=tree_runs,
                        anchor=resolved_anchor,
                    )
                    if resolved_anchor is not None
                    else ()
                )
                self._delete_retry_suffix(
                    tree_runs=tree_runs,
                    steps=trimmed,
                )
                self._insert_control(
                    scope="run",
                    target=run_id,
                    index=index,
                    kind="retry",
                    timing="immediate",
                    payload=RetryControlPayload(
                        resources=resources,
                        limits=limits,
                        state=state,
                        runnable=runnable,
                        model=model,
                        locals=locals,
                        retry_from=resolved_anchor,
                        sandbox=sandbox,
                    ),
                    request=request_id,
                    status="applied",
                    error=None,
                    created_at=created_at,
                    finished_at=created_at,
                    claimed=True,
                )
                self._conn.execute(
                    """
                    UPDATE controls
                    SET status = 'wontapply',
                        error = 'run retried before the control could be applied',
                        finished_at = ?,
                        _revision = ?
                    WHERE scope = 'run' AND target IN ({}) AND status = 'pending'
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
                    SET control_target = ?, control_index = ?,
                        status = 'pending', output = NULL, error = NULL,
                        started_at = NULL, finished_at = NULL
                    WHERE id = ?
                    """,
                    (run_id, index, run_id),
                )
                updated_run_row = self._conn.execute(
                    "SELECT * FROM runs WHERE id = ?", (run_id,)
                ).fetchone()
                control_row = self._conn.execute(
                    'SELECT * FROM controls WHERE target = ? AND "index" = ?',
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
            trimmed,
        )

    def begin_run(
        self,
        *,
        run_id: str,
        started_at: str,
        control: ControlRef,
        occurrence: Occurrence | None = None,
    ) -> RunRecord:
        """Project run execution beginning."""

        with self.write_transaction():
            self._conn.execute(
                """
                UPDATE runs
                SET status = 'running',
                    started_at = COALESCE(started_at, ?)
                WHERE id = ? AND control_target = ? AND control_index = ?
                  AND status IN ('pending', 'running')
                """,
                (started_at, run_id, control.target, control.index),
            )
            row = self._conn.execute(
                "SELECT * FROM runs WHERE id = ?", (run_id,)
            ).fetchone()
        if row is None:
            raise ValueError(f"run not found: {run_id}")
        run = _run_from_row(row)
        if (
            run.started_at != started_at
            or run.control != control
            or run.occurrence != occurrence
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
        """Mark pending run controls consumed by one execution event as applied."""

        control_indexes = tuple(dict.fromkeys(int(index) for index in indexes))
        if not control_indexes:
            return
        placeholders = ", ".join("?" for _ in control_indexes)
        with self.write_transaction():
            pending = self._conn.execute(
                f"""
                SELECT 1 FROM controls
                WHERE scope = 'run' AND target = ? AND "index" IN ({placeholders})
                  AND status = 'pending'
                LIMIT 1
                """,
                (run_id, *control_indexes),
            ).fetchone()
            if pending is None:
                return
            self._conn.execute(
                f"""
                UPDATE controls
                SET status = 'applied', finished_at = ?, _revision = ?
                WHERE scope = 'run' AND target = ?
                  AND "index" IN ({placeholders}) AND status = 'pending'
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
                SELECT 1 FROM controls
                WHERE scope = 'run' AND target = ? AND status = 'pending'
                LIMIT 1
                """,
                (run_id,),
            ).fetchone()
            if pending is None:
                return
            self._conn.execute(
                """
                UPDATE controls
                SET status = 'wontapply', error = ?, finished_at = ?, _revision = ?
                WHERE scope = 'run' AND target = ? AND status = 'pending'
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
        """Revoke one pending steer or cancel control."""

        with self.write_transaction():
            row = self._conn.execute(
                "SELECT * FROM controls WHERE scope = 'run' AND target = ? AND \"index\" = ?",
                (run_id, index),
            ).fetchone()
            if row is None:
                raise ValueError(f"run control not found: {run_id}:{index}")
            control = _run_control_from_row(row)
            if control.kind in {"run", "rerun"}:
                raise ValueError("run entry controls cannot be canceled")
            if control.status != "pending":
                raise ValueError(f"run control is not pending: {run_id}:{index}")
            if bool(row["_claimed"]):
                raise ValueError(
                    f"run control is already being applied: {run_id}:{index}"
                )
            self._conn.execute(
                """
                UPDATE controls
                SET status = 'revoked', finished_at = ?, _revision = ?
                WHERE scope = 'run' AND target = ? AND "index" = ?
                  AND status = 'pending'
                """,
                (
                    canceled_at,
                    self._next_run_control_revision(),
                    run_id,
                    index,
                ),
            )
            updated = self._conn.execute(
                "SELECT * FROM controls WHERE scope = 'run' AND target = ? AND \"index\" = ?",
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

        validate_execution_id(run_id, label="run id")
        control_indexes = tuple(dict.fromkeys(int(index) for index in indexes))
        if not control_indexes:
            return set()
        placeholders = ", ".join("?" for _ in control_indexes)
        with self.write_transaction():
            rows = self._conn.execute(
                f"""
                UPDATE controls
                SET _claimed = 1
                WHERE scope = 'run' AND target = ? AND "index" IN ({placeholders})
                  AND status = 'pending' AND _claimed = 0
                RETURNING "index"
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
        created_at: str | None = None,
    ) -> tuple[ThreadRecord, ThreadControlRecord]:
        """Atomically create one thread and its create control."""

        validate_execution_id(thread_id, label="thread id")
        _validate_request_id(request_id)
        now = created_at or utc_now()
        effective_peer = peer or ThreadPeer()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                if request_id is not None and (
                    self._conn.execute(
                        "SELECT 1 FROM controls WHERE request = ?",
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
                self._insert_control(
                    scope="thread",
                    target=thread_id,
                    index=0,
                    kind="create",
                    timing="immediate",
                    payload=CreateControlPayload(),
                    request=request_id,
                    status="applied",
                    error=None,
                    created_at=now,
                    finished_at=now,
                    claimed=True,
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
                    'SELECT * FROM controls WHERE target = ? AND "index" = 0',
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
        created_at: str,
    ) -> tuple[ThreadRecord, ThreadControlRecord]:
        """Atomically fork from one terminal anchor without copying runs."""

        validate_execution_id(thread_id, label="thread id")
        validate_execution_id(source, label="source thread id")
        if anchor is not None:
            validate_execution_id(anchor, label="anchor run id")
        _validate_request_id(request_id)
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                if request_id is not None and (
                    self._conn.execute(
                        "SELECT 1 FROM controls WHERE request = ?",
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
                self._insert_control(
                    scope="thread",
                    target=thread_id,
                    index=0,
                    kind="fork",
                    timing="immediate",
                    payload=ForkControlPayload(
                        fork_from=source_record.thread_id,
                        fork_at=anchor_record.id,
                    ),
                    request=request_id,
                    status="applied",
                    error=None,
                    created_at=created_at,
                    finished_at=created_at,
                    claimed=True,
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
                    'SELECT * FROM controls WHERE target = ? AND "index" = 0',
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
        expected_head: ControlRef,
        created_at: str,
    ) -> tuple[ThreadRecord, ThreadControlRecord, tuple[str, ...]]:
        """Atomically rewind one idle thread using optimistic head comparison."""

        validate_execution_id(thread_id, label="thread id")
        if anchor is not None:
            validate_execution_id(anchor, label="anchor run id")
        _validate_request_id(request_id)
        index: int | None = None
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                if request_id is not None and (
                    self._conn.execute(
                        "SELECT 1 FROM controls WHERE request = ?",
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
                    "FROM controls WHERE target = ?",
                    (thread_id,),
                ).fetchone()
                index = int(index_row["next_index"]) if index_row is not None else 0
                self._insert_control(
                    scope="thread",
                    target=thread_id,
                    index=index,
                    kind="rewind",
                    timing="immediate",
                    payload=RewindControlPayload(
                        rewind_from=anchor_record.id,
                        rewind_if=expected_head.index,
                    ),
                    request=request_id,
                    status="applied",
                    error=None,
                    created_at=created_at,
                    finished_at=created_at,
                    claimed=True,
                )
                if str(anchor["thread"]) == thread_id:
                    rows = self._conn.execute(
                        """
                        SELECT id FROM runs
                        WHERE thread = ?
                          AND rowid >= ?
                          AND ejected_by_target IS NULL
                        ORDER BY rowid ASC
                        """,
                        (thread_id, int(anchor["rowid"])),
                    ).fetchall()
                else:
                    rows = self._conn.execute(
                        """
                        SELECT id FROM runs
                        WHERE thread = ?
                          AND ejected_by_target IS NULL
                        ORDER BY rowid ASC
                        """,
                        (thread_id,),
                    ).fetchall()
                ejected = tuple(str(row["id"]) for row in rows)
                self._conn.executemany(
                    """
                    UPDATE runs
                    SET ejected_by_target = ?, ejected_by_index = ?
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
                    'SELECT * FROM controls WHERE target = ? AND "index" = ?',
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
                "SELECT * FROM controls WHERE scope = 'thread' AND target = ? AND \"index\" = ?",
                (thread_id, index),
            ).fetchone()
        return _thread_control_from_row(row) if row is not None else None

    def list_thread_controls(
        self, *, thread_id: str
    ) -> tuple[ThreadControlRecord, ...]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM controls WHERE scope = 'thread' AND target = ? ORDER BY \"index\" ASC",
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
        status: RunStatus = "succeeded",
        error: ExecutionError | None = None,
        finished_at: str | None = None,
        output: Local | None = None,
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
                    _dump_execution_error(error),
                    _dump_json(local_to_data(output)) if output is not None else None,
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

    def root_run_id(self, *, run_id: str) -> str:
        """Derive one run tree's root by following durable parent ownership."""

        seen: set[str] = set()
        current = run_id
        while current not in seen:
            seen.add(current)
            run = self.get_run(run_id=current)
            if run is None:
                raise ValueError(f"run ancestry is missing: {current}")
            if run.parent is None:
                return run.id
            current = run.parent.run
        raise ValueError(f"run ancestry contains a cycle: {run_id}")

    def list_run_tree(self, *, root_run_id: str) -> list[RunRecord]:
        """Return all runs structurally owned by one root run."""

        with self._lock:
            run_ids = self._root_tree_runs(root_run_id)
            if not run_ids:
                return []
            placeholders = ", ".join("?" for _ in run_ids)
            rows = self._conn.execute(
                f"SELECT * FROM runs WHERE id IN ({placeholders}) ORDER BY rowid ASC",
                run_ids,
            ).fetchall()
        return [_run_from_row(row) for row in rows]

    def run_output(self, *, run_id: str) -> tuple[Part, ...]:
        """Return the message parts represented by one run's durable output."""

        run = self.get_run(run_id=run_id)
        if run is None:
            raise ValueError(f"run not found: {run_id}")
        if run.output is None:
            return ()
        return parts_from_local(self.resolve_local(run.output))

    def resolve_local(self, local: Local) -> Local:
        """Resolve and validate every pointer in one durable typed local."""

        type_name = local.type
        value = cast(Value | TypedPointer, self.resolve_value(local.value))
        validate_runtime_value(value, type_name)
        return Local(
            value=value,
            name=local.name,
            dim=local.dim,
        )

    def resolve_value(self, value: object) -> object:
        """Resolve every immutable pointer contained in one durable value."""

        return self._resolve_value(value, seen=set())

    def resolve_error(self, error: ExecutionError) -> str:
        """Resolve one run or step error pointer to its concrete message."""

        return self._resolve_error(error, seen=set())

    def control_scope(self, ref: ControlRef) -> Literal["run", "thread"]:
        """Return the durable scope of one globally addressed control."""

        with self._lock:
            row = self._conn.execute(
                'SELECT scope FROM controls WHERE target = ? AND "index" = ?',
                (ref.target, ref.index),
            ).fetchone()
        if row is None:
            raise ValueError(f"control not found: {ref.target}^{ref.index}")
        return cast(Literal["run", "thread"], row["scope"])

    def _resolve_error(
        self,
        error: ExecutionError,
        *,
        seen: set[Pointer],
    ) -> str:
        if isinstance(error, str):
            return error
        if error in seen:
            raise ValueError(f"error pointer cycle: {error}")
        if "^" in error.anchor:
            raise ValueError(f"error pointer cannot target a control: {error}")
        if error.pointer:
            raise ValueError(f"error pointer cannot select a value: {error}")
        seen.add(error)
        try:
            target, *raw_indices = error.anchor.split(".")
            if raw_indices:
                path = StepPath(target, tuple(int(index) for index in raw_indices))
                with self._lock:
                    row = self._conn.execute(
                        "SELECT * FROM steps WHERE run = ? AND path = ?",
                        (path.run, path.local),
                    ).fetchone()
                source = _step_from_row(row).error if row is not None else None
            else:
                run = self.get_run(run_id=target)
                source = run.error if run is not None else None
            if source is None:
                raise ValueError(f"error pointer target has no error: {error}")
            return self._resolve_error(source, seen=seen)
        finally:
            seen.remove(error)

    def _resolve_value(self, value: object, *, seen: set[Pointer]) -> object:
        if isinstance(value, TypedPointer):
            pointer = value.pointer
            if pointer in seen:
                raise ValueError(f"value pointer cycle: {pointer}")
            seen.add(pointer)
            try:
                result = self._resolve_pointer(pointer, seen=seen)
                validate_runtime_value(result, value.type, path=f"pointer {pointer}")
                return result
            finally:
                seen.remove(pointer)
        if isinstance(value, Array):
            return Array(
                value.type,
                tuple(self._resolve_value(item, seen=seen) for item in value),
            )
        if isinstance(value, Struct):
            return Struct(
                value.type,
                {
                    str(name): self._resolve_value(item, seen=seen)
                    for name, item in value.items()
                },
            )
        if isinstance(value, Mapping):
            return {
                str(name): self._resolve_value(item, seen=seen)
                for name, item in value.items()
            }
        if isinstance(value, tuple | list):
            return tuple(self._resolve_value(item, seen=seen) for item in value)
        return value

    def _resolve_pointer(self, pointer: Pointer, *, seen: set[Pointer]) -> object:
        anchor = pointer.anchor
        suffix = pointer.pointer
        if "^" in anchor:
            target, raw_index = anchor.split("^", 1)
            control = self.get_run_control(run_id=target, index=int(raw_index))
            if control is None or not isinstance(
                control.payload,
                RunControlPayload
                | RerunControlPayload
                | RetryControlPayload
                | SteerControlPayload
                | CancelControlPayload,
            ):
                raise ValueError(f"control value pointer target is missing: {pointer}")
            segments = _json_pointer_segments(suffix)
            if not segments:
                raise ValueError(f"control value pointer requires a local: {pointer}")
            locals_value = control.payload.locals
            if locals_value is None:
                raise ValueError(f"control locals are inherited: {pointer}")
            local = next(
                (item for item in locals_value if item.name == segments[0]),
                None,
            )
            if local is None:
                raise ValueError(f"control local is missing: {pointer}")
            value = self._resolve_value(local.value, seen=seen)
            return _select_json_value(value, segments[1:], source=pointer)
        target, *raw_indices = anchor.split(".")
        local: Local | None
        if raw_indices:
            path = StepPath(target, tuple(int(index) for index in raw_indices))
            with self._lock:
                row = self._conn.execute(
                    "SELECT * FROM steps WHERE run = ? AND path = ?",
                    (path.run, path.local),
                ).fetchone()
            local = _step_from_row(row).output if row is not None else None
        else:
            run = self.get_run(run_id=target)
            local = run.output if run is not None else None
        if local is None:
            raise ValueError(f"value pointer target has no output: {pointer}")
        value = self._resolve_value(local.value, seen=seen)
        return _select_json_value(
            value,
            _json_pointer_segments(suffix),
            source=pointer,
        )

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
                    "ejected_by_target IS NULL",
                    "(parent IS NULL OR parent NOT IN ("
                    "SELECT run || '.' || path FROM steps "
                    "WHERE ejected_by_target IS NOT NULL))",
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
                    "ejected_by_target IS NULL",
                    "(parent IS NULL OR parent NOT IN ("
                    "SELECT run || '.' || path FROM steps "
                    "WHERE ejected_by_target IS NOT NULL))",
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
                    "SELECT * FROM controls WHERE scope = 'thread' "
                    'ORDER BY target ASC, "index" ASC'
                ).fetchall()
                run_rows = self._conn.execute(
                    "SELECT * FROM runs ORDER BY rowid ASC"
                ).fetchall()
                ejected_step_rows = self._conn.execute(
                    "SELECT run, path FROM steps WHERE ejected_by_target IS NOT NULL"
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
        ejected_steps = {
            str(StepPath.from_local(str(row["run"]), str(row["path"])))
            for row in ejected_step_rows
        }
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
                        and isinstance(created_by.payload, ForkControlPayload)
                    ):
                        source = history(
                            created_by.payload.fork_from,
                            include_hidden=True,
                            visited=visited,
                        )
                        for run in source:
                            prefix.append(run)
                            if run.id == created_by.payload.fork_at:
                                break
                        else:
                            raise ValueError(
                                "fork anchor is missing from source history: "
                                f"{created_by.payload.fork_at}"
                            )
                    if prefix:
                        positions = {
                            run.id: position for position, run in enumerate(prefix)
                        }
                        cuts = tuple(
                            positions[control.payload.rewind_from]
                            for control in controls
                            if control.kind == "rewind"
                            and isinstance(control.payload, RewindControlPayload)
                            and control.payload.rewind_from in positions
                        )
                        if cuts:
                            prefix = prefix[: min(cuts)]
                own = [
                    run
                    for run in runs_by_thread.get(thread_id, ())
                    if include_hidden
                    or (
                        run.ejected_by is None
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
        if anchor.status not in {"succeeded", "failed", "canceled"}:
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
        run_status: RunStatus,
    ) -> StepPath | None:
        placeholders = ", ".join("?" for _ in tree_runs)
        rows = self._conn.execute(
            f"""
            SELECT rowid, * FROM steps
            WHERE run IN ({placeholders})
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
                raise ValueError(f"retry anchor not found in run {run_id}: {anchor}")
            candidate = match
        else:
            incomplete = tuple(
                row
                for row in rows
                if str(row["status"]) in {"running", "failed", "canceled"}
            )
            candidate = incomplete[-1] if incomplete else None
            if candidate is None and run_status == "succeeded":
                candidate = next(
                    (row for row in reversed(rows) if str(row["kind"]) != "value"),
                    rows[-1] if rows else None,
                )
            elif candidate is None:
                candidate = rows[-1] if rows else None
        if candidate is None:
            return None
        selected = StepPath.from_local(str(candidate["run"]), str(candidate["path"]))
        resolved = self._root_retry_step(
            run_id=run_id,
            tree_runs=tree_runs,
            selected=selected,
        )
        paths = {StepPath.from_local(str(row["run"]), str(row["path"])) for row in rows}
        if resolved not in paths:
            raise ValueError(f"retry resume step not found: {resolved}")
        return resolved

    def _root_retry_step(
        self,
        *,
        run_id: str,
        tree_runs: Sequence[str],
        selected: StepPath,
    ) -> StepPath:
        """Map one tree Step to its owning top-level root Step."""

        placeholders = ", ".join("?" for _ in tree_runs)
        run_rows = self._conn.execute(
            f"SELECT * FROM runs WHERE id IN ({placeholders})",
            tuple(tree_runs),
        ).fetchall()
        parents = {
            run.id: run.parent
            for run in (_run_from_row(row) for row in run_rows)
            if run.parent is not None
        }
        current = selected
        visited: set[str] = set()
        while current.run != run_id:
            if current.run in visited:
                raise ValueError(f"cyclic retry run ancestry: {selected}")
            visited.add(current.run)
            parent = parents.get(current.run)
            if parent is None:
                raise ValueError(
                    f"retry step is not owned by root run {run_id}: {selected}"
                )
            current = parent
        return StepPath(run_id, current.indices[:1])

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
            WHERE run IN ({placeholders})
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
        if root is not None and rows:
            control_row = self._conn.execute(
                'SELECT * FROM controls WHERE target = ? AND "index" = ?',
                (root.control.target, root.control.index),
            ).fetchone()
            control = (
                _run_control_from_row(control_row) if control_row is not None else None
            )
            if (
                control is not None
                and isinstance(
                    control.payload,
                    RunControlPayload | RerunControlPayload | RetryControlPayload,
                )
                and control.payload.runnable.startswith("agic:")
            ):
                cutoff = int(rows[0]["rowid"])
        current: StepPath | None = anchor
        while current is not None:
            for size in range(1, len(current.indices) + 1):
                ancestor = StepPath(current.run, current.indices[:size])
                row = keyed.get(ancestor)
                if row is not None and str(row["status"]) != "succeeded":
                    cutoff = min(cutoff, int(row["rowid"]))
            current = parents.get(current.run)
        return tuple(
            StepPath.from_local(str(row["run"]), str(row["path"]))
            for row in rows
            if int(row["rowid"]) >= cutoff
        )

    def _delete_retry_suffix(
        self,
        *,
        tree_runs: Sequence[str],
        steps: Sequence[StepPath],
    ) -> None:
        """Delete a retry suffix and every child run it owns."""

        if not steps:
            return
        step_keys = {str(step) for step in steps}
        placeholders = ", ".join("?" for _ in tree_runs)
        run_rows = self._conn.execute(
            f"SELECT * FROM runs WHERE id IN ({placeholders}) ORDER BY rowid ASC",
            tuple(tree_runs),
        ).fetchall()
        runs = tuple(_run_from_row(row) for row in run_rows)
        removed_runs = {
            run.id
            for run in runs
            if run.parent is not None and str(run.parent) in step_keys
        }
        changed = True
        while changed:
            changed = False
            for run in runs:
                if (
                    run.id not in removed_runs
                    and run.parent is not None
                    and run.parent.run in removed_runs
                ):
                    removed_runs.add(run.id)
                    changed = True
        if removed_runs:
            removed_placeholders = ", ".join("?" for _ in removed_runs)
            removed_params = tuple(removed_runs)
            self._conn.execute(
                f"DELETE FROM steps WHERE run IN ({removed_placeholders})",
                removed_params,
            )
            self._conn.execute(
                f"DELETE FROM runs WHERE id IN ({removed_placeholders})",
                removed_params,
            )
        self._conn.executemany(
            "DELETE FROM steps WHERE run = ? AND path = ?",
            ((step.run, step.local) for step in steps),
        )

    def begin_step(
        self,
        *,
        path: StepPath,
        kind: StepKind,
        input: Sequence[Pointer],
        occurrence: Occurrence | None = None,
        given: StepGiven,
        started_at: str,
    ) -> StepRecord:
        """Project one step_begin event."""

        with self.write_transaction():
            stored_given: StoredStepGiven = (
                self.capture_model_call(model=given.model, call=given.call)
                if isinstance(given, ModelStepGiven)
                else cast(StoredStepGiven, given)
            )
            given_data = stored_step_given_to_data(kind, stored_given)
            self._conn.execute(
                """
                INSERT INTO steps(
                    run, path, kind, input, output, occurrence, given, noted,
                    status, error, created_at, started_at, finished_at
                ) VALUES (?, ?, ?, ?, NULL, ?, ?, 'null', 'running', NULL, ?, ?, NULL)
                ON CONFLICT(run, path) DO NOTHING
                """,
                (
                    path.run,
                    path.local,
                    kind,
                    _dump_json(pointers_to_data(tuple(input))),
                    _dump_json(occurrence_to_data(occurrence))
                    if occurrence is not None
                    else None,
                    _dump_json(given_data),
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
            or step.occurrence != occurrence
            or step.given != stored_given
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
        output: Local | None,
        noted: StepNoted,
        error: ExecutionError | None,
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
                        _dump_json(local_to_data(output))
                        if output is not None
                        else None,
                        _dump_json(step_noted_to_data(kind, noted)),
                        status,
                        _dump_execution_error(error),
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
            or step.output != output
            or step.noted != noted
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
            clauses.append("ejected_by_target IS NULL")
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
        visible = "" if include_ejected else "AND ejected_by_target IS NULL"
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
        model: str,
        call: ModelCall,
    ) -> StoredModelStepGiven:
        """Persist deduplicated normalized model-call inputs."""

        with self.write_transaction():
            instruction_ref = self._put_model_text(call.instructions)
            message_refs = [
                self._put_model_message(message) for message in call.messages
            ]
            toolset_ref = self._put_toolset(call.tools) if call.tools else None
        from .records import ModelCallRefs

        return StoredModelStepGiven(
            model=model,
            call=ModelCallRefs(
                instructions=instruction_ref,
                messages=tuple(message_refs),
                tools=toolset_ref,
                cont=dict(call.cont) if call.cont is not None else None,
            ),
        )

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
            if not isinstance(step.given, StoredModelStepGiven):
                raise ValueError(f"model call metadata is missing: {step.path}")
            call = step.given.call
            instruction_ref = call.instructions
            message_refs = call.messages
            toolset_ref = call.tools
            cont = dict(call.cont) if call.cont is not None else None
            references[step.path] = (
                instruction_ref,
                message_refs,
                toolset_ref,
                cont,
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
            cont,
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
                cont=cont,
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
            for item in inputs:
                if item.kind not in {"run", "rerun", "steer"} or not isinstance(
                    item.payload,
                    PreparationControlPayload | SteerControlPayload,
                ):
                    continue
                locals_value = item.payload.locals
                if locals_value is None:
                    continue
                primary = next(
                    (local for local in locals_value if local.name == "_"), None
                )
                if primary is None:
                    continue
                parts = parts_from_local(self.resolve_local(primary))
                if parts:
                    results.append(Message(role="user", parts=parts))
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
                "SELECT * FROM controls WHERE scope = 'run' AND target = ? AND \"index\" = ?",
                (run_id, index),
            ).fetchone()
        return _run_control_from_row(row) if row is not None else None

    def list_run_controls(
        self,
        *,
        run_id: str,
        kind: ControlKind | None = None,
    ) -> tuple[RunControlRecord, ...]:
        clauses = ["scope = 'run'", "target = ?"]
        params: list[object] = [run_id]
        if kind is not None:
            clauses.append("kind = ?")
            params.append(kind)
        where = " AND ".join(clauses)
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT * FROM controls
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
        kind: ControlKind | None = None,
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
                    SELECT * FROM controls
                    WHERE scope = 'run' AND target IN ({placeholders}){kind_clause}
                    ORDER BY target ASC, "index" ASC
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
        kind: ControlKind,
    ) -> tuple[RunControlRecord, ...]:
        """Return accepted run_controls not yet consumed by an execution event."""

        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM controls
                WHERE scope = 'run' AND target = ? AND kind = ?
                  AND status = 'pending'
                ORDER BY "index" ASC
                """,
                (run_id, kind),
            ).fetchall()
        return tuple(_run_control_from_row(row) for row in rows)

    def latest_run_control_revision(self) -> int:
        """Return the latest durable run-control change revision."""

        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(_revision), 0) AS sequence FROM controls"
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
                SELECT * FROM controls
                WHERE scope = 'run' AND _revision > ?
                ORDER BY _revision ASC, target ASC, "index" ASC
                """,
                (after_revision,),
            ).fetchall()
        if not rows:
            return after_revision, ()
        return (
            max(int(row["_revision"]) for row in rows),
            tuple(_run_control_from_row(row) for row in rows),
        )

    def _next_run_control_revision(self) -> int:
        row = self._conn.execute(
            "SELECT COALESCE(MAX(_revision), 0) + 1 AS revision FROM controls"
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
                    control_target TEXT NOT NULL,
                    control_index INTEGER NOT NULL,
                    output TEXT,
                    occurrence TEXT,
                    status TEXT NOT NULL,
                    error TEXT,
                    ejected_by_target TEXT,
                    ejected_by_index INTEGER,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS controls (
                    scope TEXT NOT NULL,
                    target TEXT NOT NULL,
                    "index" INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    request TEXT,
                    status TEXT NOT NULL,
                    error TEXT,
                    timing TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    finished_at TEXT,
                    _claimed INTEGER NOT NULL DEFAULT 0,
                    _revision INTEGER NOT NULL,
                    PRIMARY KEY(target, "index")
                )
                """
            )
            self._conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_controls_request
                ON controls(request)
                WHERE request IS NOT NULL
                """
            )
            self._conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_controls_revision
                ON controls(_revision)
                """
            )
            _create_steps_table(self._conn)
            if version == 28:
                _migrate_model_cont(self._conn)
            if version in {28, 29}:
                self._conn.execute(
                    "UPDATE controls SET kind = 'run' WHERE kind = 'start'"
                )
                self._conn.execute(
                    "UPDATE controls SET kind = 'cancel' WHERE kind = 'stop'"
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
        CREATE TABLE IF NOT EXISTS steps (
            run TEXT NOT NULL,
            path TEXT NOT NULL,
            kind TEXT NOT NULL,
            input TEXT NOT NULL,
            output TEXT,
            occurrence TEXT,
            given TEXT NOT NULL,
            noted TEXT NOT NULL,
            status TEXT NOT NULL,
            error TEXT,
            created_at TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            ejected_by_target TEXT,
            ejected_by_index INTEGER,
            PRIMARY KEY(run, path)
        )
        """
    )


def _migrate_model_cont(connection: sqlite3.Connection) -> None:
    """Rename model continuation keys without touching Agent State records."""

    rows = connection.execute(
        "SELECT run, path, given, noted FROM steps WHERE kind = 'model'"
    ).fetchall()
    for row in rows:
        given = _load_json(str(row["given"]))
        noted = _load_json(str(row["noted"]))
        changed = _rename_cont_key(given, nested="call")
        changed = _rename_cont_key(noted) or changed
        if not changed:
            continue
        connection.execute(
            "UPDATE steps SET given = ?, noted = ? WHERE run = ? AND path = ?",
            (
                _dump_json(given),
                _dump_json(noted),
                str(row["run"]),
                str(row["path"]),
            ),
        )


def _rename_cont_key(value: object, *, nested: str | None = None) -> bool:
    if not isinstance(value, dict):
        return False
    document = cast(dict[str, object], value)
    target = document.get(nested) if nested is not None else document
    if not isinstance(target, dict) or "state" not in target:
        return False
    target = cast(dict[str, object], target)
    if "cont" in target:
        raise ValueError("model continuation contains both state and cont")
    target["cont"] = target.pop("state")
    return True


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


def _load_stored_object(value: object, *, label: str) -> dict[str, Any]:
    if value is None:
        raise ValueError(f"stored {label} must be an object")
    decoded = _load_json(str(value))
    if not isinstance(decoded, Mapping) or not all(
        isinstance(name, str) for name in decoded
    ):
        raise ValueError(f"stored {label} must be an object")
    return dict(cast(Mapping[str, Any], decoded))


def _load_optional_stored_object(
    value: object,
    *,
    label: str,
) -> dict[str, Any] | None:
    return None if value is None else _load_stored_object(value, label=label)


def _load_stored_array(value: object, *, label: str) -> list[Any]:
    if value is None:
        raise ValueError(f"stored {label} must be an array")
    decoded = _load_json(str(value))
    if not isinstance(decoded, list):
        raise ValueError(f"stored {label} must be an array")
    return decoded


def _dump_execution_error(error: ExecutionError | None) -> str | None:
    return _dump_json(execution_error_to_data(error)) if error is not None else None


def _load_execution_error(value: object) -> ExecutionError | None:
    if value is None:
        return None
    return execution_error_from_data(_load_json(str(value)))


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


def _validate_request_id(request_id: str | None) -> None:
    if request_id is not None and (
        not request_id.strip() or request_id != request_id.strip()
    ):
        raise ValueError(f"invalid request id: {request_id!r}")


def _validate_canonical_sandbox(sandbox: str | None) -> None:
    if not isinstance(sandbox, str) or not sandbox or sandbox != sandbox.strip():
        raise ValueError("root run requires a canonical sandbox")


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
    output_data = _load_optional_stored_object(
        row["output"],
        label="run output",
    )
    occurrence_data = _load_optional_stored_object(
        row["occurrence"],
        label="run occurrence",
    )
    return RunRecord(
        id=str(row["id"]),
        parent=(
            StepPath.parse(str(row["parent"])) if row["parent"] is not None else None
        ),
        thread=str(row["thread"]),
        control=ControlRef(
            target=str(row["control_target"]),
            index=int(row["control_index"]),
        ),
        output=(local_from_data(output_data) if output_data is not None else None),
        occurrence=occurrence_from_data(occurrence_data),
        status=cast(RunStatus, row["status"]),
        error=_load_execution_error(row["error"]),
        ejected_by=(
            ControlRef(
                target=str(row["ejected_by_target"]),
                index=int(row["ejected_by_index"]),
            )
            if row["ejected_by_target"] is not None
            and row["ejected_by_index"] is not None
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
        created_by=ControlRef(
            target=str(raw["thread_id"]),
            index=int(cast(int | str, raw["created_by_index"])),
        ),
        head=ControlRef(
            target=str(raw["thread_id"]),
            index=int(cast(int | str, raw["head_index"])),
        ),
        created_at=str(raw["created_at"]),
        updated_at=str(raw["updated_at"]),
    )


def _step_from_row(row: sqlite3.Row) -> StepRecord:
    raw = dict(row)
    kind = _step_kind_from_data(raw["kind"])
    input_data = _load_stored_array(raw["input"], label="step input")
    output_data = _load_optional_stored_object(
        raw["output"],
        label="step output",
    )
    occurrence_data = _load_optional_stored_object(
        raw["occurrence"],
        label="step occurrence",
    )
    given_data = _load_stored_object(raw["given"], label="step given")
    if raw["noted"] is None:
        raise ValueError("stored step noted must be JSON null or an object")
    noted_data = _load_json(str(raw["noted"]))
    return StepRecord(
        path=StepPath.from_local(str(raw["run"]), str(raw["path"])),
        kind=kind,
        input=pointers_from_data(input_data),
        output=(local_from_data(output_data) if output_data is not None else None),
        occurrence=occurrence_from_data(occurrence_data),
        given=stored_step_given_from_data(kind, given_data),
        noted=step_noted_from_data(kind, noted_data),
        status=cast(StepStatus, raw["status"]),
        error=_load_execution_error(raw["error"]),
        ejected_by=(
            ControlRef(
                target=str(raw["ejected_by_target"]),
                index=int(cast(int | str, raw["ejected_by_index"])),
            )
            if raw.get("ejected_by_target") is not None
            and raw.get("ejected_by_index") is not None
            else None
        ),
        created_at=str(raw["created_at"]),
        started_at=str(raw["started_at"]),
        finished_at=str(raw["finished_at"]) if raw["finished_at"] is not None else None,
    )


def _step_kind_from_data(value: object) -> StepKind:
    if not isinstance(value, str) or value not in {
        "run",
        "agent",
        "human",
        "model",
        "tool",
        "par",
        "loop",
        "value",
    }:
        raise ValueError(f"invalid stored step kind: {value!r}")
    return cast(StepKind, value)


def _run_control_from_row(row: sqlite3.Row) -> RunControlRecord:
    kind = cast(
        Literal["run", "rerun", "retry", "steer", "cancel"],
        row["kind"],
    )
    payload = cast(
        RunScopedControlPayload,
        control_payload_from_data(kind, _load_json(str(row["payload"]))),
    )
    return RunControlRecord(
        target=str(row["target"]),
        index=int(row["index"]),
        kind=kind,
        payload=payload,
        timing=cast(ControlTiming, row["timing"]),
        request=str(row["request"]) if row["request"] is not None else None,
        status=cast(ControlStatus, row["status"]),
        error=str(row["error"]) if row["error"] is not None else None,
        created_at=str(row["created_at"]),
        finished_at=str(row["finished_at"]) if row["finished_at"] is not None else None,
    )


def _thread_control_from_row(row: sqlite3.Row) -> ThreadControlRecord:
    kind = cast(Literal["create", "fork", "rewind"], row["kind"])
    payload = cast(
        ThreadControlPayload,
        control_payload_from_data(kind, _load_json(str(row["payload"]))),
    )
    return ThreadControlRecord(
        target=str(row["target"]),
        index=int(row["index"]),
        kind=kind,
        payload=payload,
        request=str(row["request"]) if row["request"] is not None else None,
        status=cast(ControlStatus, row["status"]),
        created_at=str(row["created_at"]),
        finished_at=str(row["finished_at"]) if row["finished_at"] is not None else None,
    )


def _replay_messages_from_step(step: StepRecord) -> list[Message]:
    role = step_message_role(step.kind)
    if role is None or not step.output:
        return []
    parts = parts_from_local(step.output)
    return [Message(role=role, parts=parts)] if parts else []


def _json_pointer_segments(pointer: str | None) -> tuple[str, ...]:
    if not pointer:
        return ()
    return tuple(
        segment.replace("~1", "/").replace("~0", "~") for segment in pointer.split("/")
    )


def _select_json_value(
    value: object,
    segments: Sequence[str],
    *,
    source: Pointer,
) -> object:
    current = value
    for segment in segments:
        if isinstance(current, Mapping):
            if segment not in current:
                raise ValueError(f"value pointer key is missing: {source}")
            current = cast(Mapping[str, object], current)[segment]
            continue
        if isinstance(current, Sequence) and not isinstance(
            current, (str, bytes, bytearray)
        ):
            if not segment.isascii() or not segment.isdigit():
                raise ValueError(f"value pointer index is invalid: {source}")
            index = int(segment)
            if index >= len(current):
                raise ValueError(f"value pointer index is out of range: {source}")
            current = current[index]
            continue
        raise ValueError(f"value pointer traverses a scalar: {source}")
    return current


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
