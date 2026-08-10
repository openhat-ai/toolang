"""SQLite-backed checkpoints for ready task and chore jobs."""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
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
from toolang.execution.types import RunStatus

from .errors import JobStoreSchemaError
from .records import JobRecord
from .state import Job, schedule_revision
from .types import JobStatus, JobTrigger

_SCHEMA_VERSION = 3


@dataclass(frozen=True, slots=True)
class ClaimedJob:
    """One activation durably assigned to a preallocated run id."""

    job: Job
    record: JobRecord
    trigger: JobTrigger
    active_at: str


class JobStore:
    """Own durable scheduler transitions; due discovery stays in memory."""

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
                db_path.as_posix(), check_same_thread=False, timeout=30
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

    def reconcile(
        self,
        *,
        jobs: Mapping[str, Job] | Iterable[Job],
        now: datetime | None = None,
    ) -> tuple[JobRecord, ...]:
        """Project the latest effective ready definitions into checkpoints."""

        current = _utc(now)
        indexed = _index_jobs(jobs)
        with self._write():
            for job in indexed.values():
                self._upsert_locked(job, now=current)
            for row in self._conn.execute("SELECT * FROM jobs").fetchall():
                record = _record(row)
                if record.job_id in indexed or record.active_run_id is not None:
                    continue
                self._conn.execute(
                    "DELETE FROM jobs WHERE job_id = ?",
                    (record.job_id,),
                )
            rows = self._conn.execute(
                "SELECT * FROM jobs ORDER BY kind, job_id"
            ).fetchall()
        return tuple(_record(row) for row in rows)

    def get(self, *, job_id: str, kind: JobKind | None = None) -> JobRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        if row is None:
            return None
        record = _record(row)
        return record if kind is None or record.kind == kind else None

    def get_by_run(self, *, run_id: str) -> JobRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM jobs WHERE active_run_id = ?",
                (run_id,),
            ).fetchone()
        return _record(row) if row is not None else None

    def list(self, *, kind: JobKind | None = None) -> tuple[JobRecord, ...]:
        with self._lock:
            rows = (
                self._conn.execute(
                    "SELECT * FROM jobs ORDER BY kind, job_id"
                ).fetchall()
                if kind is None
                else self._conn.execute(
                    "SELECT * FROM jobs WHERE kind = ? ORDER BY job_id",
                    (kind,),
                ).fetchall()
            )
        return tuple(_record(row) for row in rows)

    def claim(
        self,
        *,
        job: Job,
        trigger: JobTrigger,
        run_id: str,
        now: datetime | None = None,
    ) -> ClaimedJob:
        """Claim one known due activation without searching for another job."""

        current = _utc(now)
        with self._write():
            row = self._require_row_locked(job.id)
            record = _record(row)
            _require_kind(record, job)
            if record.active_run_id is not None:
                raise ValueError(f"job is already running: {job.id}")
            active_at, ready_at, next_run_at = _consume_activation(
                record,
                job,
                trigger=trigger,
                now=current,
            )
            self._conn.execute(
                """
                UPDATE jobs
                SET revision = ?, status = 'running', ready_at = ?,
                    active_run_id = ?, active_revision = ?, active_trigger = ?,
                    active_at = ?, next_run_at = ?, error = NULL, updated_at = ?
                WHERE job_id = ? AND active_run_id IS NULL
                """,
                (
                    job.revision,
                    ready_at,
                    run_id,
                    job.revision,
                    trigger,
                    _iso(active_at),
                    next_run_at,
                    _iso(current),
                    job.id,
                ),
            )
            updated = self._require_row_locked(job.id)
        return ClaimedJob(
            job=job,
            record=_record(updated),
            trigger=trigger,
            active_at=_iso(active_at),
        )

    def reject_activation(
        self,
        *,
        job: Job,
        trigger: JobTrigger,
        error: str,
        now: datetime | None = None,
    ) -> JobRecord:
        """Consume one invalid activation without creating a run."""

        current = _utc(now)
        with self._write():
            record = _record(self._require_row_locked(job.id))
            _require_kind(record, job)
            if record.active_run_id is not None:
                raise ValueError(f"job is already running: {job.id}")
            _active_at, ready_at, next_run_at = _consume_activation(
                record,
                job,
                trigger=trigger,
                now=current,
            )
            status = _rejected_status(job.kind, ready_at, next_run_at)
            self._conn.execute(
                """
                UPDATE jobs
                SET revision = ?, status = ?, ready_at = ?, next_run_at = ?,
                    error = ?, updated_at = ?
                WHERE job_id = ?
                """,
                (
                    job.revision,
                    status,
                    ready_at,
                    next_run_at,
                    error,
                    _iso(current),
                    job.id,
                ),
            )
            updated = self._require_row_locked(job.id)
        return _record(updated)

    def reject_claim(
        self,
        *,
        run_id: str,
        error: str,
        now: datetime | None = None,
    ) -> JobRecord | None:
        """Finish a claimed activation rejected by RunExecutor acceptance."""

        current = _utc(now)
        with self._write():
            row = self._conn.execute(
                "SELECT * FROM jobs WHERE active_run_id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                return None
            record = _record(row)
            status = _finished_status(record, "failed")
            self._conn.execute(
                """
                UPDATE jobs
                SET status = ?, active_run_id = NULL, active_revision = NULL,
                    active_trigger = NULL, active_at = NULL, error = ?, updated_at = ?
                WHERE job_id = ? AND active_run_id = ?
                """,
                (status, error, _iso(current), record.job_id, run_id),
            )
            updated = self._require_row_locked(record.job_id)
        return _record(updated)

    def release_claim(
        self,
        *,
        run_id: str,
        now: datetime | None = None,
    ) -> JobRecord | None:
        """Restore an activation whose preallocated run was never accepted."""

        current = _utc(now)
        with self._write():
            row = self._conn.execute(
                "SELECT * FROM jobs WHERE active_run_id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                return None
            record = _record(row)
            ready_at = record.ready_at
            next_run_at = record.next_run_at
            if record.active_trigger in {"source", "manual"}:
                ready_at = _earliest(ready_at, record.active_at)
            elif record.active_trigger == "schedule":
                next_run_at = _earliest(next_run_at, record.active_at)
            self._conn.execute(
                """
                UPDATE jobs
                SET status = 'pending', ready_at = ?, next_run_at = ?,
                    active_run_id = NULL, active_revision = NULL,
                    active_trigger = NULL, active_at = NULL, updated_at = ?
                WHERE job_id = ? AND active_run_id = ?
                """,
                (ready_at, next_run_at, _iso(current), record.job_id, run_id),
            )
            updated = self._require_row_locked(record.job_id)
        return _record(updated)

    def mark_recovery_blocked(
        self,
        *,
        run_id: str,
        error: str,
        now: datetime | None = None,
    ) -> JobRecord | None:
        """Retain an ambiguous accepted run without risking duplicate dispatch."""

        current = _utc(now)
        with self._write():
            cursor = self._conn.execute(
                """
                UPDATE jobs
                SET error = ?, updated_at = ?
                WHERE active_run_id = ?
                """,
                (error, _iso(current), run_id),
            )
            if cursor.rowcount != 1:
                return None
            row = self._conn.execute(
                "SELECT * FROM jobs WHERE active_run_id = ?",
                (run_id,),
            ).fetchone()
        return _record(row) if row is not None else None

    def finish_run(
        self,
        *,
        jobs: Mapping[str, Job],
        run_id: str,
        run_status: RunStatus,
        now: datetime | None = None,
    ) -> JobRecord | None:
        """Apply one terminal run result to its active checkpoint."""

        if run_status in {"pending", "running"}:
            raise ValueError(f"run is not terminal: {run_status}")
        current = _utc(now)
        with self._write():
            row = self._conn.execute(
                "SELECT * FROM jobs WHERE active_run_id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                return None
            record = _record(row)
            job = jobs.get(record.job_id)
            if job is None:
                self._conn.execute(
                    "DELETE FROM jobs WHERE job_id = ?",
                    (record.job_id,),
                )
                return None
            _require_kind(record, job)
            status = _finished_status(record, run_status)
            self._conn.execute(
                """
                UPDATE jobs
                SET revision = ?, status = ?, active_run_id = NULL,
                    active_revision = NULL, active_trigger = NULL,
                    active_at = NULL, error = NULL, updated_at = ?
                WHERE job_id = ? AND active_run_id = ?
                """,
                (job.revision, status, _iso(current), record.job_id, run_id),
            )
            updated = self._require_row_locked(record.job_id)
        return _record(updated)

    def reopen_task(
        self,
        *,
        task_id: str,
        now: datetime | None = None,
    ) -> JobRecord:
        """Request the current task revision again."""

        current = _utc(now)
        with self._write():
            record = _record(self._require_row_locked(task_id))
            if record.kind != "task":
                raise ValueError(f"job is not a task: {task_id}")
            if record.active_run_id is not None:
                raise ValueError(f"task is running: {task_id}")
            if record.status not in {"done", "failed", "canceled"}:
                raise ValueError(f"task cannot reopen from status: {record.status}")
            self._conn.execute(
                """
                UPDATE jobs
                SET status = 'pending', ready_at = ?, error = NULL, updated_at = ?
                WHERE job_id = ?
                """,
                (_iso(current), _iso(current), task_id),
            )
            updated = self._require_row_locked(task_id)
        return _record(updated)

    def request_manual_chore(
        self,
        *,
        chore_id: str,
        now: datetime | None = None,
    ) -> JobRecord:
        """Keep at most one pending manual occurrence for a ready chore."""

        current = _utc(now)
        with self._write():
            record = _record(self._require_row_locked(chore_id))
            if record.kind != "chore":
                raise ValueError(f"job is not a chore: {chore_id}")
            ready_at = record.ready_at or _iso(current)
            status = "running" if record.active_run_id is not None else "pending"
            self._conn.execute(
                """
                UPDATE jobs
                SET status = ?, ready_at = ?, error = NULL, updated_at = ?
                WHERE job_id = ?
                """,
                (status, ready_at, _iso(current), chore_id),
            )
            updated = self._require_row_locked(chore_id)
        return _record(updated)

    def cancel_pending_task(
        self,
        *,
        task_id: str,
        now: datetime | None = None,
    ) -> JobRecord:
        """Cancel a task activation that has not started."""

        current = _utc(now)
        with self._write():
            record = _record(self._require_row_locked(task_id))
            if record.kind != "task":
                raise ValueError(f"job is not a task: {task_id}")
            if record.active_run_id is not None or record.status != "pending":
                raise ValueError(f"task cannot cancel from status: {record.status}")
            self._conn.execute(
                """
                UPDATE jobs
                SET status = 'canceled', ready_at = NULL, updated_at = ?
                WHERE job_id = ?
                """,
                (_iso(current), task_id),
            )
            updated = self._require_row_locked(task_id)
        return _record(updated)

    def _upsert_locked(self, job: Job, *, now: datetime) -> None:
        row = self._conn.execute(
            "SELECT * FROM jobs WHERE job_id = ?",
            (job.id,),
        ).fetchone()
        current_schedule_revision = schedule_revision(job)
        if row is None:
            anchor = _iso(now) if job.kind == "chore" else None
            next_run_at = _initial_next_run(job, anchor=now, now=now)
            self._conn.execute(
                """
                INSERT INTO jobs(
                    job_id, kind, revision, status, ready_at,
                    active_run_id, active_revision, active_trigger, active_at,
                    schedule_revision, schedule_anchor, next_run_at, error,
                    created_at, updated_at
                ) VALUES (?, ?, ?, 'pending', ?, NULL, NULL, NULL, NULL,
                          ?, ?, ?, NULL, ?, ?)
                """,
                (
                    job.id,
                    job.kind,
                    job.revision,
                    _iso(now) if job.kind == "task" else None,
                    current_schedule_revision,
                    anchor,
                    next_run_at,
                    _iso(now),
                    _iso(now),
                ),
            )
            return
        record = _record(row)
        _require_kind(record, job)
        status = record.status
        ready_at = record.ready_at
        anchor = record.schedule_anchor
        next_run_at = record.next_run_at
        error = record.error
        if job.kind == "task" and job.revision != record.revision:
            ready_at = ready_at or _iso(now)
            status = "running" if record.active_run_id is not None else "pending"
            error = None
        elif job.kind == "chore":
            if current_schedule_revision != record.schedule_revision:
                anchor = _iso(now)
                next_run_at = _initial_next_run(job, anchor=now, now=now)
                if record.active_run_id is None:
                    status = "pending" if ready_at or next_run_at else "done"
                error = None
            elif job.revision != record.revision:
                error = None
        self._conn.execute(
            """
            UPDATE jobs
            SET revision = ?, status = ?, ready_at = ?, schedule_revision = ?,
                schedule_anchor = ?, next_run_at = ?, error = ?, updated_at = ?
            WHERE job_id = ?
            """,
            (
                job.revision,
                status,
                ready_at,
                current_schedule_revision,
                anchor,
                next_run_at,
                error,
                _iso(now),
                job.id,
            ),
        )

    def _require_row_locked(self, job_id: str) -> sqlite3.Row:
        row = self._conn.execute(
            "SELECT * FROM jobs WHERE job_id = ?",
            (job_id,),
        ).fetchone()
        if row is None:
            raise FileNotFoundError(f"job not found: {job_id}")
        return row

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.execute("PRAGMA busy_timeout=30000;")
            version = int(self._conn.execute("PRAGMA user_version").fetchone()[0])
            if version > _SCHEMA_VERSION or (
                self.read_only and version != _SCHEMA_VERSION
            ):
                raise JobStoreSchemaError(
                    version,
                    current=_SCHEMA_VERSION,
                    read_only=self.read_only,
                )
            if self.read_only:
                self._conn.execute("PRAGMA query_only=ON;")
                return
            self._conn.execute("PRAGMA journal_mode=WAL;")
            self._conn.execute("PRAGMA synchronous=NORMAL;")
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                version = int(self._conn.execute("PRAGMA user_version").fetchone()[0])
                if version > _SCHEMA_VERSION:
                    raise JobStoreSchemaError(
                        version,
                        current=_SCHEMA_VERSION,
                        read_only=False,
                    )
                if version < _SCHEMA_VERSION:
                    self._conn.execute("DROP TABLE IF EXISTS jobs")
                self._conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS jobs (
                        job_id TEXT PRIMARY KEY,
                        kind TEXT NOT NULL,
                        revision TEXT NOT NULL,
                        status TEXT NOT NULL,
                        ready_at TEXT,
                        active_run_id TEXT,
                        active_revision TEXT,
                        active_trigger TEXT,
                        active_at TEXT,
                        schedule_revision TEXT,
                        schedule_anchor TEXT,
                        next_run_at TEXT,
                        error TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )
                    """
                )
                self._conn.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_active_run
                    ON jobs(active_run_id)
                    WHERE active_run_id IS NOT NULL
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
            except BaseException:
                self._conn.rollback()
                raise
            else:
                self._conn.commit()


def jobs_db_path(layout: AgentLayout) -> Path:
    """Return the scheduler checkpoint database path."""

    return layout.job_store


def open_job_store(layout: AgentLayout, *, read_only: bool = False) -> JobStore:
    """Open scheduler checkpoints for one agent."""

    return JobStore(jobs_db_path(layout), read_only=read_only)


def next_activation(record: JobRecord) -> tuple[datetime, JobTrigger] | None:
    """Return one record's next in-memory heap activation."""

    if record.active_run_id is not None:
        return None
    candidates: list[tuple[datetime, JobTrigger]] = []
    if record.ready_at is not None:
        candidates.append(
            (
                _parse(record.ready_at),
                "source" if record.kind == "task" else "manual",
            )
        )
    if record.kind == "chore" and record.next_run_at is not None:
        candidates.append((_parse(record.next_run_at), "schedule"))
    return min(candidates, default=None, key=lambda item: (item[0], item[1]))


def _consume_activation(
    record: JobRecord,
    job: Job,
    *,
    trigger: JobTrigger,
    now: datetime,
) -> tuple[datetime, str | None, str | None]:
    ready_at = record.ready_at
    next_run_at = record.next_run_at
    if trigger == "source":
        if job.kind != "task" or ready_at is None or _parse(ready_at) > now:
            raise ValueError(f"task is not due: {job.id}")
        active_at = _parse(ready_at)
        ready_at = None
    elif trigger == "manual":
        if job.kind != "chore" or ready_at is None or _parse(ready_at) > now:
            raise ValueError(f"chore has no manual request: {job.id}")
        active_at = _parse(ready_at)
        ready_at = None
    else:
        if (
            job.kind != "chore"
            or job.schedule is None
            or record.schedule_anchor is None
            or next_run_at is None
            or _parse(next_run_at) > now
        ):
            raise ValueError(f"chore schedule is not due: {job.id}")
        rule = rrulestr(job.schedule, dtstart=_parse(record.schedule_anchor))
        scheduled = rule.before(now, inc=True)
        if scheduled is None:
            raise ValueError(f"chore schedule has no due occurrence: {job.id}")
        active_at = _utc(scheduled)
        following = rule.after(active_at, inc=False)
        next_run_at = _iso(_utc(following)) if following is not None else None
    return active_at, ready_at, next_run_at


def _initial_next_run(job: Job, *, anchor: datetime, now: datetime) -> str | None:
    if job.kind != "chore" or job.schedule is None:
        return None
    rule = rrulestr(job.schedule, dtstart=_utc(anchor))
    candidate = rule.after(_utc(now), inc=True)
    return _iso(_utc(candidate)) if candidate is not None else None


def _finished_status(record: JobRecord, run_status: RunStatus) -> JobStatus:
    if record.kind == "chore":
        return "pending" if record.ready_at or record.next_run_at else "done"
    if record.ready_at is not None or record.revision != record.active_revision:
        return "pending"
    if run_status == "finished":
        return "done"
    if run_status == "canceled":
        return "canceled"
    return "failed"


def _rejected_status(
    kind: JobKind,
    ready_at: str | None,
    next_run_at: str | None,
) -> JobStatus:
    if kind == "task":
        return "failed"
    return "pending" if ready_at or next_run_at else "done"


def _require_kind(record: JobRecord, job: Job) -> None:
    if record.kind != job.kind:
        raise ValueError(f"job kind changed for {job.id}: {record.kind} -> {job.kind}")


def _index_jobs(jobs: Mapping[str, Job] | Iterable[Job]) -> dict[str, Job]:
    values: Iterable[Job]
    if isinstance(jobs, Mapping):
        values = cast(Mapping[str, Job], jobs).values()
    else:
        values = jobs
    indexed: dict[str, Job] = {}
    for job in values:
        if job.id in indexed:
            raise ValueError(f"duplicate job id: {job.id}")
        indexed[job.id] = job
    return indexed


def _record(row: sqlite3.Row) -> JobRecord:
    return JobRecord(
        job_id=str(row["job_id"]),
        kind=cast(JobKind, str(row["kind"])),
        revision=str(row["revision"]),
        status=cast(JobStatus, str(row["status"])),
        ready_at=_optional(row["ready_at"]),
        active_run_id=_optional(row["active_run_id"]),
        active_revision=_optional(row["active_revision"]),
        active_trigger=cast(JobTrigger | None, _optional(row["active_trigger"])),
        active_at=_optional(row["active_at"]),
        schedule_revision=_optional(row["schedule_revision"]),
        schedule_anchor=_optional(row["schedule_anchor"]),
        next_run_at=_optional(row["next_run_at"]),
        error=_optional(row["error"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _optional(value: object) -> str | None:
    return None if value is None else str(value)


def _earliest(left: str | None, right: str | None) -> str | None:
    if left is None:
        return right
    if right is None:
        return left
    return min(left, right)


def _utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return _utc(value).isoformat()


def _parse(value: str) -> datetime:
    return _utc(datetime.fromisoformat(value))
