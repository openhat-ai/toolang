"""SQLite-backed scheduler state for ready task and chore jobs."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import threading
from typing import cast

from dateutil.rrule import rrulestr

from toolang.catalog.types import JobKind
from toolang.common.layout import AgentLayout
from ..execution.types import RunStatus
from .records import JobRecord
from .types import JobStatus, JobTrigger
from .state import AgentJobs, JobDefinition

_SCHEMA_VERSION = 2


@dataclass(frozen=True, slots=True)
class ClaimedJob:
    """One atomically claimed job ready for execution."""

    job: JobRecord
    definition: JobDefinition
    trigger: JobTrigger


class JobStore:
    """Scheduler state and claim store for ready jobs."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            db_path.as_posix(), check_same_thread=False, timeout=30
        )
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._init_schema()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def reconcile(
        self,
        *,
        jobs: AgentJobs,
        kind: JobKind | None = None,
        now: datetime | None = None,
    ) -> tuple[JobRecord, ...]:
        """Reconcile one immutable definition snapshot into durable state."""

        current = _utc(now)
        seen: set[tuple[str, JobKind]] = set()
        records: list[JobRecord] = []
        with self._write():
            for definition in jobs.definitions:
                if kind is not None and definition.kind != kind:
                    continue
                key = (definition.id, definition.kind)
                if key in seen:
                    continue
                seen.add(key)
                record = self._upsert_definition_locked(
                    definition=definition,
                    now=current,
                )
                records.append(record)
            self._delete_missing_locked(kind=kind, seen=seen)
        return tuple(records)

    def get(self, *, job_id: str, kind: JobKind | None = None) -> JobRecord | None:
        with self._lock:
            if kind is None:
                row = self._conn.execute(
                    "SELECT * FROM jobs WHERE job_id = ? ORDER BY kind LIMIT 1",
                    (job_id,),
                ).fetchone()
            else:
                row = self._conn.execute(
                    "SELECT * FROM jobs WHERE job_id = ? AND kind = ?",
                    (job_id, kind),
                ).fetchone()
        return _job_from_row(row) if row is not None else None

    def get_by_thread(self, *, thread_id: str) -> JobRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM jobs WHERE thread_id = ?",
                (thread_id,),
            ).fetchone()
        return _job_from_row(row) if row is not None else None

    def get_by_run(self, *, run_id: str) -> JobRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM jobs WHERE last_run_id = ?",
                (run_id,),
            ).fetchone()
        return _job_from_row(row) if row is not None else None

    def list(self, *, kind: JobKind | None = None) -> tuple[JobRecord, ...]:
        with self._lock:
            if kind is None:
                rows = self._conn.execute(
                    "SELECT * FROM jobs ORDER BY kind, job_id",
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM jobs WHERE kind = ? ORDER BY job_id",
                    (kind,),
                ).fetchall()
        return tuple(_job_from_row(row) for row in rows)

    def claim_due(
        self,
        *,
        jobs: AgentJobs,
        kind: JobKind,
        now: datetime | None = None,
        manual: bool = False,
    ) -> ClaimedJob | None:
        """Atomically claim one due ready job of the requested kind."""

        current = _utc(now)
        with self._write():
            row = self._select_claimable_locked(kind=kind, now=current, manual=manual)
            if row is None:
                return None
            job = _job_from_row(row)
            definition = jobs.get(job.kind, job.job_id)
            if definition is None:
                self._conn.execute(
                    "DELETE FROM jobs WHERE job_id = ? AND kind = ?",
                    (job.job_id, job.kind),
                )
                return None
            trigger: JobTrigger = "manual" if manual else "scheduler"
            cursor = self._conn.execute(
                """
                UPDATE jobs
                SET status = 'running', last_run_id = NULL, updated_at = ?
                WHERE job_id = ? AND kind = ? AND status = 'todo'
                """,
                (_iso(current), job.job_id, job.kind),
            )
            if cursor.rowcount != 1:
                return None
            updated = self._conn.execute(
                "SELECT * FROM jobs WHERE job_id = ? AND kind = ?",
                (job.job_id, job.kind),
            ).fetchone()
        if updated is None:
            return None
        claimed = _job_from_row(updated)
        return ClaimedJob(
            job=claimed,
            definition=definition,
            trigger=trigger,
        )

    def claim_chore_manual(
        self,
        *,
        jobs: AgentJobs,
        chore_id: str,
        now: datetime | None = None,
    ) -> ClaimedJob:
        """Atomically claim one ready chore for a manual run."""

        self.reconcile(jobs=jobs, kind="chore", now=now)
        current = _utc(now)
        with self._write():
            row = self._conn.execute(
                "SELECT * FROM jobs WHERE job_id = ? AND kind = 'chore'",
                (chore_id,),
            ).fetchone()
            if row is None:
                raise FileNotFoundError(f"chore not found: {chore_id}")
            job = _job_from_row(row)
            if job.status != "todo":
                raise ValueError(f"chore cannot run from status: {job.status}")
            definition = jobs.get("chore", chore_id)
            if definition is None:
                raise FileNotFoundError(f"chore not found: {chore_id}")
            cursor = self._conn.execute(
                """
                UPDATE jobs
                SET status = 'running', last_run_id = NULL, updated_at = ?
                WHERE job_id = ? AND kind = 'chore' AND status = 'todo'
                """,
                (_iso(current), chore_id),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"chore cannot run from status: {job.status}")
            updated = self._conn.execute(
                "SELECT * FROM jobs WHERE job_id = ? AND kind = 'chore'",
                (chore_id,),
            ).fetchone()
        if updated is None:
            raise RuntimeError(f"chore not found after claim: {chore_id}")
        return ClaimedJob(
            job=_job_from_row(updated),
            definition=definition,
            trigger="manual",
        )

    def bind_run(
        self,
        *,
        job_id: str,
        kind: JobKind,
        run_id: str,
        now: datetime | None = None,
    ) -> JobRecord:
        """Bind an executor-assigned run id to one active job claim."""

        current = _utc(now)
        with self._write():
            try:
                cursor = self._conn.execute(
                    """
                    UPDATE jobs
                    SET last_run_id = ?, updated_at = ?
                    WHERE job_id = ? AND kind = ?
                      AND status = 'running' AND last_run_id IS NULL
                    """,
                    (run_id, _iso(current), job_id, kind),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError(f"job run already bound: {run_id}") from exc
            if cursor.rowcount != 1:
                raise ValueError(f"job claim cannot bind run: {kind}:{job_id}")
            updated = self._conn.execute(
                "SELECT * FROM jobs WHERE job_id = ? AND kind = ?",
                (job_id, kind),
            ).fetchone()
        if updated is None:
            raise RuntimeError(f"job not found after run binding: {kind}:{job_id}")
        return _job_from_row(updated)

    def reject_claim(
        self,
        *,
        jobs: AgentJobs,
        job_id: str,
        kind: JobKind,
        now: datetime | None = None,
    ) -> JobRecord:
        """Finish a claimed job whose submission was rejected before a run."""

        current = _utc(now)
        with self._write():
            row = self._conn.execute(
                "SELECT * FROM jobs WHERE job_id = ? AND kind = ?",
                (job_id, kind),
            ).fetchone()
            if row is None:
                raise FileNotFoundError(f"job not found: {kind}:{job_id}")
            job = _job_from_row(row)
            if job.status != "running" or job.last_run_id is not None:
                raise ValueError(f"job claim cannot be rejected: {kind}:{job_id}")
            status, next_run_at = _completed_job_state(
                jobs,
                job,
                run_status="failed",
                now=current,
            )
            self._conn.execute(
                """
                UPDATE jobs
                SET status = ?, next_run_at = ?, run_count = run_count + 1,
                    failed_count = failed_count + 1, updated_at = ?
                WHERE job_id = ? AND kind = ?
                """,
                (status, next_run_at, _iso(current), job_id, kind),
            )
            updated = self._conn.execute(
                "SELECT * FROM jobs WHERE job_id = ? AND kind = ?",
                (job_id, kind),
            ).fetchone()
        if updated is None:
            raise RuntimeError(f"job not found after rejection: {kind}:{job_id}")
        return _job_from_row(updated)

    def reopen_task(
        self,
        *,
        jobs: AgentJobs,
        task_id: str,
        now: datetime | None = None,
    ) -> JobRecord:
        """Set a non-running ready task back to todo."""

        self.reconcile(jobs=jobs, kind="task", now=now)
        current = _utc(now)
        with self._write():
            row = self._conn.execute(
                "SELECT * FROM jobs WHERE job_id = ? AND kind = 'task'",
                (task_id,),
            ).fetchone()
            if row is None:
                raise FileNotFoundError(f"task not found: {task_id}")
            job = _job_from_row(row)
            if job.status not in ("done", "failed", "canceled"):
                raise ValueError(f"task cannot be reopened from status: {job.status}")
            self._conn.execute(
                "UPDATE jobs SET status = 'todo', updated_at = ? WHERE job_id = ? AND kind = 'task'",
                (_iso(current), task_id),
            )
            updated = self._conn.execute(
                "SELECT * FROM jobs WHERE job_id = ? AND kind = 'task'",
                (task_id,),
            ).fetchone()
        if updated is None:
            raise RuntimeError(f"task not found after reopen: {task_id}")
        return _job_from_row(updated)

    def cancel_pending_task(
        self, *, task_id: str, now: datetime | None = None
    ) -> JobRecord:
        """Mark a pending task canceled without creating a run."""

        current = _utc(now)
        with self._write():
            row = self._conn.execute(
                "SELECT * FROM jobs WHERE job_id = ? AND kind = 'task'",
                (task_id,),
            ).fetchone()
            if row is None:
                raise FileNotFoundError(f"task not found: {task_id}")
            job = _job_from_row(row)
            if job.status != "todo":
                raise ValueError(f"task cannot be canceled from status: {job.status}")
            self._conn.execute(
                "UPDATE jobs SET status = 'canceled', updated_at = ? WHERE job_id = ? AND kind = 'task'",
                (_iso(current), task_id),
            )
            updated = self._conn.execute(
                "SELECT * FROM jobs WHERE job_id = ? AND kind = 'task'",
                (task_id,),
            ).fetchone()
        if updated is None:
            raise RuntimeError(f"task not found after cancel: {task_id}")
        return _job_from_row(updated)

    def finish_run(
        self,
        *,
        jobs: AgentJobs,
        run_id: str,
        run_status: RunStatus,
        now: datetime | None = None,
    ) -> JobRecord | None:
        """Update one job after its current run reaches a terminal status."""

        current = _utc(now)
        with self._write():
            row = self._conn.execute(
                "SELECT * FROM jobs WHERE last_run_id = ? AND status = 'running'",
                (run_id,),
            ).fetchone()
            if row is None:
                return None
            job = _job_from_row(row)
            status, next_run_at = _completed_job_state(
                jobs,
                job,
                run_status=run_status,
                now=current,
            )
            failed_inc = 1 if run_status == "failed" else 0
            canceled_inc = 1 if run_status == "canceled" else 0
            self._conn.execute(
                """
                UPDATE jobs
                SET status = ?,
                    next_run_at = ?,
                    run_count = run_count + 1,
                    failed_count = failed_count + ?,
                    canceled_count = canceled_count + ?,
                    updated_at = ?
                WHERE job_id = ? AND kind = ?
                """,
                (
                    status,
                    next_run_at,
                    failed_inc,
                    canceled_inc,
                    _iso(current),
                    job.job_id,
                    job.kind,
                ),
            )
            updated = self._conn.execute(
                "SELECT * FROM jobs WHERE job_id = ? AND kind = ?",
                (job.job_id, job.kind),
            ).fetchone()
        return _job_from_row(updated) if updated is not None else None

    def _upsert_definition_locked(
        self,
        *,
        definition: JobDefinition,
        now: datetime,
    ) -> JobRecord:
        job_id = definition.id
        kind = definition.kind
        definition_hash = definition.fingerprint
        thread_id = definition.thread
        path = definition.source
        row = self._conn.execute(
            "SELECT * FROM jobs WHERE job_id = ? AND kind = ?",
            (job_id, kind),
        ).fetchone()
        if row is None:
            status: JobStatus = "todo"
            next_run_at = _initial_next_run(definition, now=now)
            self._conn.execute(
                """
                INSERT INTO jobs(
                    job_id, kind, path, definition_hash, thread_id, status,
                    last_run_id, next_run_at, run_count, failed_count, canceled_count,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    kind,
                    path,
                    definition_hash,
                    thread_id,
                    status,
                    None,
                    next_run_at,
                    0,
                    0,
                    0,
                    _iso(now),
                    _iso(now),
                ),
            )
        else:
            current = _job_from_row(row)
            status = current.status
            next_run_at = current.next_run_at
            if kind == "chore" and (
                current.definition_hash != definition_hash or next_run_at is None
            ):
                next_run_at = _initial_next_run(definition, now=now)
            self._conn.execute(
                """
                UPDATE jobs
                SET path = ?, definition_hash = ?, thread_id = ?, status = ?,
                    next_run_at = ?, updated_at = ?
                WHERE job_id = ? AND kind = ?
                """,
                (
                    path,
                    definition_hash,
                    thread_id,
                    status,
                    next_run_at,
                    _iso(now),
                    job_id,
                    kind,
                ),
            )
        updated = self._conn.execute(
            "SELECT * FROM jobs WHERE job_id = ? AND kind = ?",
            (job_id, kind),
        ).fetchone()
        if updated is None:
            raise RuntimeError(f"job not found after upsert: {kind}:{job_id}")
        return _job_from_row(updated)

    def _delete_missing_locked(
        self,
        *,
        kind: JobKind | None,
        seen: set[tuple[str, JobKind]],
    ) -> None:
        rows = (
            self._conn.execute("SELECT job_id, kind FROM jobs").fetchall()
            if kind is None
            else self._conn.execute(
                "SELECT job_id, kind FROM jobs WHERE kind = ?", (kind,)
            ).fetchall()
        )
        for row in rows:
            key = (str(row["job_id"]), cast(JobKind, str(row["kind"])))
            if key not in seen:
                self._conn.execute(
                    "DELETE FROM jobs WHERE job_id = ? AND kind = ?",
                    key,
                )

    def _select_claimable_locked(
        self,
        *,
        kind: JobKind,
        now: datetime,
        manual: bool,
    ) -> sqlite3.Row | None:
        if kind == "task":
            return self._conn.execute(
                """
                SELECT * FROM jobs
                WHERE kind = 'task' AND status = 'todo'
                ORDER BY created_at
                LIMIT 1
                """
            ).fetchone()
        if manual:
            return self._conn.execute(
                """
                SELECT * FROM jobs
                WHERE kind = 'chore' AND status = 'todo'
                ORDER BY created_at
                LIMIT 1
                """
            ).fetchone()
        return self._conn.execute(
            """
            SELECT * FROM jobs
            WHERE kind = 'chore' AND status = 'todo'
              AND next_run_at IS NOT NULL
              AND next_run_at <= ?
            ORDER BY next_run_at, created_at
            LIMIT 1
            """,
            (_iso(now),),
        ).fetchone()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL;")
            self._conn.execute("PRAGMA synchronous=NORMAL;")
            self._conn.execute("PRAGMA busy_timeout=30000;")
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                version = int(self._conn.execute("PRAGMA user_version").fetchone()[0])
                if version != _SCHEMA_VERSION:
                    self._conn.execute("DROP TABLE IF EXISTS jobs")
                self._conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS jobs (
                        job_id TEXT NOT NULL,
                        kind TEXT NOT NULL,
                        path TEXT NOT NULL,
                        definition_hash TEXT NOT NULL,
                        thread_id TEXT NOT NULL,
                        status TEXT NOT NULL,
                        last_run_id TEXT,
                        next_run_at TEXT,
                        run_count INTEGER NOT NULL,
                        failed_count INTEGER NOT NULL,
                        canceled_count INTEGER NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY(job_id, kind)
                    )
                    """
                )
                self._conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_jobs_kind_status_next ON jobs(kind, status, next_run_at)"
                )
                self._conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_jobs_thread ON jobs(thread_id)"
                )
                self._conn.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_last_run
                    ON jobs(last_run_id)
                    WHERE last_run_id IS NOT NULL
                    """
                )
                self._conn.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")
            except Exception:
                self._conn.rollback()
                raise
            else:
                self._conn.commit()

    @contextmanager
    def _write(self) -> Iterator[None]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield
            except Exception:
                self._conn.rollback()
                raise
            else:
                self._conn.commit()


def jobs_db_path(layout: AgentLayout) -> Path:
    """Return the scheduler job database path."""

    return layout.job_store


def open_job_store(layout: AgentLayout) -> JobStore:
    """Open the scheduler job store for one agent."""

    return JobStore(jobs_db_path(layout))


def _next_scheduled_at(
    schedule_text: str,
    *,
    anchor: datetime,
    not_before: datetime,
    inclusive: bool,
) -> datetime | None:
    schedule = rrulestr(schedule_text, dtstart=_utc(anchor))
    candidate = schedule.after(_utc(not_before), inc=inclusive)
    return None if candidate is None else _utc(candidate)


def _initial_next_run(definition: JobDefinition, *, now: datetime) -> str | None:
    if definition.kind == "task" or definition.schedule is None:
        return None
    return _iso(
        _next_scheduled_at(
            definition.schedule,
            anchor=now,
            not_before=now,
            inclusive=True,
        )
    )


def _completed_job_state(
    jobs: AgentJobs,
    job: JobRecord,
    *,
    run_status: RunStatus,
    now: datetime,
) -> tuple[JobStatus, str | None]:
    if job.kind == "task":
        return _task_status_from_run(run_status), None
    definition = jobs.get("chore", job.job_id)
    next_at = (
        None
        if definition is None or definition.schedule is None
        else _next_scheduled_at(
            definition.schedule,
            anchor=now,
            not_before=now,
            inclusive=False,
        )
    )
    return ("todo" if next_at is not None else "done"), _iso(next_at)


def _task_status_from_run(status: RunStatus) -> JobStatus:
    if status == "finished":
        return "done"
    if status == "canceled":
        return "canceled"
    return "failed"


def _job_from_row(row: sqlite3.Row) -> JobRecord:
    return JobRecord(
        job_id=str(row["job_id"]),
        kind=cast(JobKind, str(row["kind"])),
        path=str(row["path"]),
        definition_hash=str(row["definition_hash"]),
        thread_id=str(row["thread_id"]),
        status=cast(JobStatus, str(row["status"])),
        last_run_id=str(row["last_run_id"]) if row["last_run_id"] is not None else None,
        next_run_at=str(row["next_run_at"]) if row["next_run_at"] is not None else None,
        run_count=int(row["run_count"]),
        failed_count=int(row["failed_count"]),
        canceled_count=int(row["canceled_count"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return _utc(value).isoformat()
