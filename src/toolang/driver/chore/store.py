"""SQLite-backed checkpoints for chore definitions."""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import threading
from typing import cast

from dateutil.rrule import rrulestr

from toolang.common.layout import AgentLayout
from toolang.execution.types import RunStatus

from toolang.execution.store import RunStore
from .records import ChoreRecord
from toolang.catalog.chores import Chore
from .types import ChoreStatus, ChoreTrigger


class ChoreStore:
    """Own durable scheduler transitions; due discovery stays in memory."""

    def __init__(self, db_path: Path, *, read_only: bool = False) -> None:
        self.db_path = db_path
        self.read_only = read_only
        if not read_only:
            RunStore(db_path).close()
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
        chores: Mapping[str, Chore] | Iterable[Chore],
        now: datetime | None = None,
    ) -> tuple[ChoreRecord, ...]:
        """Project the latest effective current definitions into checkpoints."""

        current = _utc(now)
        indexed = _index_chores(chores)
        with self._write():
            for chore in indexed.values():
                self._upsert_locked(chore, now=current)
            rows = self._conn.execute(
                "SELECT * FROM chores ORDER BY name, chore_id"
            ).fetchall()
        return tuple(_record(row) for row in rows)

    def get(self, *, chore_id: str) -> ChoreRecord | None:
        with self._lock:
            if not self._has_table():
                return None
            row = self._conn.execute(
                "SELECT * FROM chores WHERE chore_id = ?",
                (chore_id,),
            ).fetchone()
        if row is None:
            return None
        record = _record(row)
        return record

    def get_by_run(self, *, run_id: str) -> ChoreRecord | None:
        with self._lock:
            if not self._has_table():
                return None
            row = self._conn.execute(
                "SELECT * FROM chores WHERE active_run_id = ?",
                (run_id,),
            ).fetchone()
        return _record(row) if row is not None else None

    def list(self) -> tuple[ChoreRecord, ...]:
        with self._lock:
            if not self._has_table():
                return ()
            rows = self._conn.execute(
                "SELECT * FROM chores ORDER BY name, chore_id"
            ).fetchall()
        return tuple(_record(row) for row in rows)

    def claim(
        self,
        *,
        chore: Chore,
        trigger: ChoreTrigger,
        run_id: str,
        now: datetime | None = None,
    ) -> ChoreRecord:
        """Claim one known due activation without searching for another chore."""

        current = _utc(now)
        with self._write():
            row = self._require_row_locked(chore.id)
            record = _record(row)
            _require_identity(record, chore)
            if record.active_run_id is not None:
                raise ValueError(f"chore is already running: {chore.id}")
            active_due_at, pending_at, next_scheduled_at = _consume_activation(
                record,
                chore,
                trigger=trigger,
                now=current,
            )
            self._conn.execute(
                """
                UPDATE chores
                SET body_hash = ?, status = 'running', pending_at = ?, pending_trigger = ?,
                    active_run_id = ?, active_trigger = ?,
                    active_due_at = ?, next_scheduled_at = ?, error = NULL, updated_at = ?
                WHERE chore_id = ? AND active_run_id IS NULL
                """,
                (
                    chore.body_hash,
                    pending_at,
                    record.pending_trigger if pending_at else None,
                    run_id,
                    trigger,
                    _iso(active_due_at),
                    next_scheduled_at,
                    _iso(current),
                    chore.id,
                ),
            )
            updated = self._require_row_locked(chore.id)
        return _record(updated)

    def reject_activation(
        self,
        *,
        chore: Chore,
        trigger: ChoreTrigger,
        error: str,
        now: datetime | None = None,
    ) -> ChoreRecord:
        """Consume one invalid activation without creating a run."""

        current = _utc(now)
        with self._write():
            record = _record(self._require_row_locked(chore.id))
            _require_identity(record, chore)
            if record.active_run_id is not None:
                raise ValueError(f"chore is already running: {chore.id}")
            _active_at, pending_at, next_scheduled_at = _consume_activation(
                record,
                chore,
                trigger=trigger,
                now=current,
            )
            status = _rejected_status(pending_at, next_scheduled_at)
            self._conn.execute(
                """
                UPDATE chores
                SET body_hash = ?, status = ?, pending_at = ?, pending_trigger = ?, next_scheduled_at = ?,
                    error = ?, updated_at = ?
                WHERE chore_id = ?
                """,
                (
                    chore.body_hash,
                    status,
                    pending_at,
                    record.pending_trigger if pending_at else None,
                    next_scheduled_at,
                    error,
                    _iso(current),
                    chore.id,
                ),
            )
            updated = self._require_row_locked(chore.id)
        return _record(updated)

    def reject_claim(
        self,
        *,
        run_id: str,
        error: str,
        now: datetime | None = None,
    ) -> ChoreRecord | None:
        """Finish a claimed activation rejected by RunExecutor acceptance."""

        current = _utc(now)
        with self._write():
            row = self._conn.execute(
                "SELECT * FROM chores WHERE active_run_id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                return None
            record = _record(row)
            status = _finished_status(record, "failed")
            self._conn.execute(
                """
                UPDATE chores
                SET status = ?, active_run_id = NULL,
                    active_trigger = NULL, active_due_at = NULL, error = ?, updated_at = ?
                WHERE chore_id = ? AND active_run_id = ?
                """,
                (status, error, _iso(current), record.chore_id, run_id),
            )
            updated = self._require_row_locked(record.chore_id)
        return _record(updated)

    def release_claim(
        self,
        *,
        run_id: str,
        now: datetime | None = None,
    ) -> ChoreRecord | None:
        """Restore an activation whose preallocated run was never accepted."""

        current = _utc(now)
        with self._write():
            row = self._conn.execute(
                "SELECT * FROM chores WHERE active_run_id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                return None
            record = _record(row)
            pending_at = record.pending_at
            pending_trigger = record.pending_trigger
            next_scheduled_at = record.next_scheduled_at
            if record.active_trigger in {"source", "manual"}:
                if pending_at is None or (
                    record.active_due_at is not None
                    and record.active_due_at < pending_at
                ):
                    pending_trigger = record.active_trigger
                pending_at = _earliest(pending_at, record.active_due_at)
            elif (
                record.active_trigger == "schedule"
                and record.schedule_anchor_at is not None
                and record.active_due_at is not None
                and _parse(record.active_due_at) > _parse(record.schedule_anchor_at)
            ):
                # A new anchor supersedes claims from the previous schedule.
                next_scheduled_at = _earliest(next_scheduled_at, record.active_due_at)
            self._conn.execute(
                """
                UPDATE chores
                SET status = ?, pending_at = ?, pending_trigger = ?, next_scheduled_at = ?,
                    active_run_id = NULL,
                    active_trigger = NULL, active_due_at = NULL, updated_at = ?
                WHERE chore_id = ? AND active_run_id = ?
                """,
                (
                    "pending" if pending_at or next_scheduled_at else "done",
                    pending_at,
                    pending_trigger,
                    next_scheduled_at,
                    _iso(current),
                    record.chore_id,
                    run_id,
                ),
            )
            updated = self._require_row_locked(record.chore_id)
        return _record(updated)

    def mark_recovery_blocked(
        self,
        *,
        run_id: str,
        error: str,
        now: datetime | None = None,
    ) -> ChoreRecord | None:
        """Retain an ambiguous accepted run without risking duplicate dispatch."""

        current = _utc(now)
        with self._write():
            cursor = self._conn.execute(
                """
                UPDATE chores
                SET error = ?, updated_at = ?
                WHERE active_run_id = ?
                """,
                (error, _iso(current), run_id),
            )
            if cursor.rowcount != 1:
                return None
            row = self._conn.execute(
                "SELECT * FROM chores WHERE active_run_id = ?",
                (run_id,),
            ).fetchone()
        return _record(row) if row is not None else None

    def finish_run(
        self,
        *,
        chores: Mapping[str, Chore],
        run_id: str,
        run_status: RunStatus,
        now: datetime | None = None,
    ) -> ChoreRecord | None:
        """Apply one terminal run result to its active checkpoint."""

        if run_status in {"pending", "running"}:
            raise ValueError(f"run is not terminal: {run_status}")
        current = _utc(now)
        with self._write():
            row = self._conn.execute(
                "SELECT * FROM chores WHERE active_run_id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                return None
            record = _record(row)
            chore = chores.get(record.chore_id)
            if chore is not None:
                _require_identity(record, chore)
            status = _finished_status(record, run_status)
            self._conn.execute(
                """
                UPDATE chores
                SET body_hash = ?, status = ?, active_run_id = NULL,
                    active_trigger = NULL,
                    active_due_at = NULL, error = NULL, updated_at = ?
                WHERE chore_id = ? AND active_run_id = ?
                """,
                (
                    chore.body_hash if chore is not None else record.body_hash,
                    status,
                    _iso(current),
                    record.chore_id,
                    run_id,
                ),
            )
            updated = self._require_row_locked(record.chore_id)
        return _record(updated)

    def request_manual_chore(
        self,
        *,
        chore_id: str,
        now: datetime | None = None,
    ) -> ChoreRecord:
        """Keep at most one pending manual occurrence for one chore."""

        current = _utc(now)
        with self._write():
            record = _record(self._require_row_locked(chore_id))
            pending_at = record.pending_at or _iso(current)
            status = "running" if record.active_run_id is not None else "pending"
            self._conn.execute(
                """
                UPDATE chores
                SET status = ?, pending_at = ?, pending_trigger = ?, error = NULL, updated_at = ?
                WHERE chore_id = ?
                """,
                (
                    status,
                    pending_at,
                    record.pending_trigger or "manual",
                    _iso(current),
                    chore_id,
                ),
            )
            updated = self._require_row_locked(chore_id)
        return _record(updated)

    def cancel_pending_chore(
        self,
        *,
        chore_id: str,
        now: datetime | None = None,
    ) -> ChoreRecord:
        """Cancel a chore activation that has not started."""

        current = _utc(now)
        with self._write():
            record = _record(self._require_row_locked(chore_id))
            if record.active_run_id is not None or record.status != "pending":
                raise ValueError(f"chore cannot cancel from status: {record.status}")
            self._conn.execute(
                """
                UPDATE chores
                SET status = CASE WHEN next_scheduled_at IS NULL THEN 'canceled' ELSE 'pending' END, pending_at = NULL, pending_trigger = NULL, updated_at = ?
                WHERE chore_id = ?
                """,
                (_iso(current), chore_id),
            )
            updated = self._require_row_locked(chore_id)
        return _record(updated)

    def _upsert_locked(self, chore: Chore, *, now: datetime) -> None:
        row = self._conn.execute(
            "SELECT * FROM chores WHERE chore_id = ?",
            (chore.id,),
        ).fetchone()
        current_schedule_hash = chore.schedule_hash
        if row is None:
            anchor = _iso(now) if chore.schedule is not None else None
            next_scheduled_at = _initial_next_run(chore, anchor=now, now=now)
            self._conn.execute(
                """
                INSERT INTO chores(
                    chore_id, name, body_hash, status, pending_at, pending_trigger,
                    active_run_id, active_trigger, active_due_at,
                    schedule_hash, schedule_anchor_at, next_scheduled_at, error,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, NULL,
                          ?, ?, ?, NULL, ?, ?)
                """,
                (
                    chore.id,
                    chore.name,
                    chore.body_hash,
                    "pending"
                    if chore.schedule is None or next_scheduled_at
                    else "done",
                    _iso(now) if chore.schedule is None else None,
                    "source" if chore.schedule is None else None,
                    current_schedule_hash,
                    anchor,
                    next_scheduled_at,
                    _iso(now),
                    _iso(now),
                ),
            )
            return
        record = _record(row)
        _require_identity(record, chore)
        if (
            chore.body_hash == record.body_hash
            and current_schedule_hash == record.schedule_hash
        ):
            return
        status = record.status
        pending_at = record.pending_at
        pending_trigger = record.pending_trigger
        anchor = record.schedule_anchor_at
        next_scheduled_at = record.next_scheduled_at
        error = record.error
        if chore.schedule is None and chore.body_hash != record.body_hash:
            pending_trigger = pending_trigger or "source"
            pending_at = pending_at or _iso(now)
            status = "running" if record.active_run_id is not None else "pending"
            error = None
        if current_schedule_hash != record.schedule_hash:
            anchor = _iso(now) if chore.schedule is not None else None
            next_scheduled_at = _initial_next_run(chore, anchor=now, now=now)
            if record.active_run_id is None:
                if pending_at or next_scheduled_at:
                    status = "pending"
                elif status == "pending":
                    status = "done"
            error = None
        elif chore.body_hash != record.body_hash:
            error = None
        self._conn.execute(
            """
            UPDATE chores
            SET body_hash = ?, status = ?, pending_at = ?, pending_trigger = ?, schedule_hash = ?,
                schedule_anchor_at = ?, next_scheduled_at = ?, error = ?, updated_at = ?
            WHERE chore_id = ?
            """,
            (
                chore.body_hash,
                status,
                pending_at,
                pending_trigger,
                current_schedule_hash,
                anchor,
                next_scheduled_at,
                error,
                _iso(now),
                chore.id,
            ),
        )

    def _require_row_locked(self, chore_id: str) -> sqlite3.Row:
        row = self._conn.execute(
            "SELECT * FROM chores WHERE chore_id = ?",
            (chore_id,),
        ).fetchone()
        if row is None:
            raise FileNotFoundError(f"chore not found: {chore_id}")
        return row

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.execute("PRAGMA busy_timeout=30000")
            if self.read_only:
                self._conn.execute("PRAGMA query_only=ON")
                return
            with self._write():
                self._conn.execute("""
                    CREATE TABLE IF NOT EXISTS chores (
                        chore_id TEXT PRIMARY KEY,
                        name TEXT NOT NULL,
                        body_hash TEXT NOT NULL,
                        status TEXT NOT NULL,
                        pending_at TEXT,
                        pending_trigger TEXT,
                        active_run_id TEXT,
                        active_trigger TEXT,
                        active_due_at TEXT,
                        schedule_hash TEXT,
                        schedule_anchor_at TEXT,
                        next_scheduled_at TEXT,
                        error TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )
                """)
                self._conn.execute("""
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_chores_active_run
                    ON chores(active_run_id) WHERE active_run_id IS NOT NULL
                """)

    def _has_table(self) -> bool:
        return (
            self._conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name = 'chores'"
            ).fetchone()
            is not None
        )

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


def open_chore_store(layout: AgentLayout, *, read_only: bool = False) -> ChoreStore:
    """Open scheduler checkpoints for one agent."""

    return ChoreStore(layout.chore_store, read_only=read_only)


def next_activation(record: ChoreRecord) -> tuple[datetime, ChoreTrigger] | None:
    """Return one record's next in-memory heap activation."""

    if record.active_run_id is not None:
        return None
    candidates: list[tuple[datetime, ChoreTrigger]] = []
    if record.pending_at is not None:
        candidates.append(
            (
                _parse(record.pending_at),
                cast(ChoreTrigger, record.pending_trigger),
            )
        )
    if record.next_scheduled_at is not None:
        candidates.append((_parse(record.next_scheduled_at), "schedule"))
    return min(candidates, default=None, key=lambda item: (item[0], item[1]))


def _consume_activation(
    record: ChoreRecord,
    chore: Chore,
    *,
    trigger: ChoreTrigger,
    now: datetime,
) -> tuple[datetime, str | None, str | None]:
    pending_at = record.pending_at
    next_scheduled_at = record.next_scheduled_at
    if trigger in {"source", "manual"}:
        if (
            pending_at is None
            or record.pending_trigger != trigger
            or _parse(pending_at) > now
        ):
            raise ValueError(f"chore has no due {trigger} request: {chore.id}")
        active_due_at = _parse(pending_at)
        pending_at = None
    else:
        if (
            chore.schedule is None
            or record.schedule_anchor_at is None
            or next_scheduled_at is None
            or _parse(next_scheduled_at) > now
        ):
            raise ValueError(f"chore schedule is not due: {chore.id}")
        rule = rrulestr(chore.schedule, dtstart=_parse(record.schedule_anchor_at))
        scheduled = rule.before(now, inc=True)
        if scheduled is None:
            raise ValueError(f"chore schedule has no due occurrence: {chore.id}")
        active_due_at = _utc(scheduled)
        following = rule.after(active_due_at, inc=False)
        next_scheduled_at = _iso(_utc(following)) if following is not None else None
    return active_due_at, pending_at, next_scheduled_at


def _initial_next_run(chore: Chore, *, anchor: datetime, now: datetime) -> str | None:
    if chore.schedule is None:
        return None
    rule = rrulestr(chore.schedule, dtstart=_utc(anchor))
    candidate = rule.after(_utc(now), inc=False)
    return _iso(_utc(candidate)) if candidate is not None else None


def _finished_status(record: ChoreRecord, run_status: RunStatus) -> ChoreStatus:
    if record.pending_at is not None or record.next_scheduled_at is not None:
        return "pending"
    if run_status == "succeeded":
        return "done"
    if run_status == "canceled":
        return "canceled"
    return "failed"


def _rejected_status(
    pending_at: str | None, next_scheduled_at: str | None
) -> ChoreStatus:
    return "pending" if pending_at or next_scheduled_at else "failed"


def _require_identity(record: ChoreRecord, chore: Chore) -> None:
    if record.name != chore.name:
        raise ValueError(f"chore ID collision: {record.name!r} and {chore.name!r}")


def _index_chores(chores: Mapping[str, Chore] | Iterable[Chore]) -> dict[str, Chore]:
    values: Iterable[Chore]
    if isinstance(chores, Mapping):
        values = cast(Mapping[str, Chore], chores).values()
    else:
        values = chores
    indexed: dict[str, Chore] = {}
    for chore in values:
        if chore.id in indexed:
            raise ValueError(f"duplicate chore id: {chore.id}")
        indexed[chore.id] = chore
    return indexed


def _record(row: sqlite3.Row) -> ChoreRecord:
    return ChoreRecord(
        chore_id=str(row["chore_id"]),
        name=str(row["name"]),
        body_hash=str(row["body_hash"]),
        status=cast(ChoreStatus, str(row["status"])),
        pending_at=_optional(row["pending_at"]),
        pending_trigger=cast(ChoreTrigger | None, _optional(row["pending_trigger"])),
        active_run_id=_optional(row["active_run_id"]),
        active_trigger=cast(ChoreTrigger | None, _optional(row["active_trigger"])),
        active_due_at=_optional(row["active_due_at"]),
        schedule_hash=_optional(row["schedule_hash"]),
        schedule_anchor_at=_optional(row["schedule_anchor_at"]),
        next_scheduled_at=_optional(row["next_scheduled_at"]),
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
