from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3

import pytest

from toolang.work.errors import JobStoreSchemaError
from toolang.work.state import Job
from toolang.work.store import JobStore, next_activation


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _schema_version(path: Path) -> int:
    connection = sqlite3.connect(path)
    try:
        return int(connection.execute("PRAGMA user_version").fetchone()[0])
    finally:
        connection.close()


def _task(revision: str = "one") -> Job:
    return Job(
        id="review",
        kind="task",
        title=None,
        body=f"Review {revision}.",
        schedule=None,
        revision=revision,
        source="tasks/review.md",
        path=None,
    )


def _chore(
    *,
    revision: str = "one",
    schedule: str = "FREQ=MINUTELY;COUNT=5",
) -> Job:
    return Job(
        id="maintain",
        kind="chore",
        title=None,
        body=f"Maintain {revision}.",
        schedule=schedule,
        revision=revision,
        source="chores/maintain.md",
        path=None,
    )


def test_job_store_rejects_a_newer_schema_without_modifying_it(tmp_path) -> None:
    path = tmp_path / "jobs.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE future_state (value TEXT NOT NULL)")
    connection.execute("INSERT INTO future_state VALUES ('preserved')")
    connection.execute("PRAGMA user_version=4")
    connection.commit()
    connection.close()

    with pytest.raises(JobStoreSchemaError) as raised:
        JobStore(path)

    assert raised.value.version == 4
    assert raised.value.current == 3
    assert _schema_version(path) == 4
    connection = sqlite3.connect(path)
    try:
        assert connection.execute("SELECT value FROM future_state").fetchone() == (
            "preserved",
        )
    finally:
        connection.close()


def test_read_only_job_store_never_migrates_an_older_schema(tmp_path) -> None:
    path = tmp_path / "jobs.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE jobs (legacy TEXT NOT NULL)")
    connection.execute("INSERT INTO jobs VALUES ('preserved')")
    connection.execute("PRAGMA user_version=2")
    connection.commit()
    connection.close()

    with pytest.raises(JobStoreSchemaError) as raised:
        JobStore(path, read_only=True)

    assert raised.value.read_only is True
    assert _schema_version(path) == 2
    connection = sqlite3.connect(path)
    try:
        assert connection.execute("SELECT legacy FROM jobs").fetchone() == (
            "preserved",
        )
    finally:
        connection.close()


def test_job_store_upgrades_an_older_schema_forward(tmp_path) -> None:
    path = tmp_path / "jobs.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE jobs (legacy TEXT NOT NULL)")
    connection.execute("PRAGMA user_version=2")
    connection.commit()
    connection.close()

    store = JobStore(path)
    store.close()

    assert _schema_version(path) == 3
    connection = sqlite3.connect(path)
    try:
        columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(jobs)")}
    finally:
        connection.close()
    assert "job_id" in columns
    assert "legacy" not in columns


def test_task_revisions_coalesce_and_run_serially(tmp_path) -> None:
    store = JobStore(tmp_path / "jobs.db")
    first = _task()
    second = replace(first, body="Review two.", revision="two")
    try:
        (pending,) = store.reconcile(jobs={first.id: first}, now=NOW)
        claimed = store.claim(
            job=first,
            trigger="source",
            run_id="run_one",
            now=NOW,
        )
        assert pending.status == "pending"
        assert claimed.record.status == "running"
        assert next_activation(claimed.record) is None

        (edited,) = store.reconcile(
            jobs={second.id: second},
            now=NOW + timedelta(seconds=1),
        )
        assert edited.status == "running"
        assert edited.revision == "two"
        assert edited.active_revision == "one"
        assert edited.ready_at == (NOW + timedelta(seconds=1)).isoformat()

        after_first = store.finish_run(
            jobs={second.id: second},
            run_id="run_one",
            run_status="succeeded",
            now=NOW + timedelta(seconds=2),
        )
        assert after_first is not None
        assert after_first.status == "pending"

        store.claim(
            job=second,
            trigger="source",
            run_id="run_two",
            now=NOW + timedelta(seconds=2),
        )
        done = store.finish_run(
            jobs={second.id: second},
            run_id="run_two",
            run_status="succeeded",
            now=NOW + timedelta(seconds=3),
        )
        assert done is not None and done.status == "done"
        assert (
            store.reconcile(
                jobs={second.id: second},
                now=NOW + timedelta(seconds=4),
            )[0].status
            == "done"
        )

        reopened = store.reopen_task(
            task_id=second.id,
            now=NOW + timedelta(seconds=5),
        )
        assert reopened.status == "pending"
        assert reopened.ready_at == (NOW + timedelta(seconds=5)).isoformat()
    finally:
        store.close()


def test_active_job_survives_ready_removal_until_run_finishes(tmp_path) -> None:
    store = JobStore(tmp_path / "jobs.db")
    job = _task()
    try:
        store.reconcile(jobs={job.id: job}, now=NOW)
        store.claim(
            job=job,
            trigger="source",
            run_id="run_one",
            now=NOW,
        )

        retained = store.reconcile(jobs={}, now=NOW + timedelta(seconds=1))
        assert len(retained) == 1
        assert retained[0].active_run_id == "run_one"

        assert (
            store.finish_run(
                jobs={},
                run_id="run_one",
                run_status="succeeded",
                now=NOW + timedelta(seconds=2),
            )
            is None
        )
        assert store.list() == ()
    finally:
        store.close()


def test_rejected_start_preserves_a_newer_pending_task_revision(tmp_path) -> None:
    store = JobStore(tmp_path / "jobs.db")
    first = _task()
    second = replace(first, body="Review two.", revision="two")
    try:
        store.reconcile(jobs={first.id: first}, now=NOW)
        store.claim(
            job=first,
            trigger="source",
            run_id="run_rejected",
            now=NOW,
        )
        store.reconcile(
            jobs={second.id: second},
            now=NOW + timedelta(seconds=1),
        )

        rejected = store.reject_claim(
            run_id="run_rejected",
            error="executor rejected the start",
            now=NOW + timedelta(seconds=2),
        )
        assert rejected is not None
        assert rejected.status == "pending"
        assert rejected.revision == "two"
        assert rejected.ready_at == (NOW + timedelta(seconds=1)).isoformat()
        assert rejected.error == "executor rejected the start"

        with pytest.raises(ValueError, match="run is not terminal"):
            store.finish_run(
                jobs={second.id: second},
                run_id="missing",
                run_status="running",
            )
    finally:
        store.close()


def test_missing_run_releases_the_original_activation(tmp_path) -> None:
    store = JobStore(tmp_path / "jobs.db")
    job = _task()
    try:
        store.reconcile(jobs={job.id: job}, now=NOW)
        claimed = store.claim(
            job=job,
            trigger="source",
            run_id="run_missing",
            now=NOW + timedelta(seconds=1),
        )
        released = store.release_claim(
            run_id="run_missing",
            now=NOW + timedelta(seconds=2),
        )

        assert released is not None
        assert released.status == "pending"
        assert released.active_run_id is None
        assert released.ready_at == claimed.active_at
        assert next_activation(released) == (NOW, "source")
    finally:
        store.close()


def test_chore_coalesces_missed_occurrences_without_shifting_schedule(
    tmp_path,
) -> None:
    store = JobStore(tmp_path / "jobs.db")
    job = _chore()
    try:
        (initial,) = store.reconcile(jobs={job.id: job}, now=NOW)
        assert next_activation(initial) == (NOW, "schedule")

        claimed = store.claim(
            job=job,
            trigger="schedule",
            run_id="run_schedule",
            now=NOW + timedelta(minutes=3, seconds=30),
        )
        assert claimed.active_at == (NOW + timedelta(minutes=3)).isoformat()
        assert claimed.record.next_run_at == (NOW + timedelta(minutes=4)).isoformat()

        waiting = store.finish_run(
            jobs={job.id: job},
            run_id="run_schedule",
            run_status="failed",
            now=NOW + timedelta(minutes=3, seconds=40),
        )
        assert waiting is not None and waiting.status == "pending"
        assert next_activation(waiting) == (
            NOW + timedelta(minutes=4),
            "schedule",
        )

        manual = store.request_manual_chore(
            chore_id=job.id,
            now=NOW + timedelta(minutes=3, seconds=45),
        )
        assert manual.next_run_at == (NOW + timedelta(minutes=4)).isoformat()
        assert next_activation(manual) == (
            NOW + timedelta(minutes=3, seconds=45),
            "manual",
        )

        store.claim(
            job=job,
            trigger="manual",
            run_id="run_manual",
            now=NOW + timedelta(minutes=3, seconds=45),
        )
        after_manual = store.finish_run(
            jobs={job.id: job},
            run_id="run_manual",
            run_status="succeeded",
            now=NOW + timedelta(minutes=3, seconds=50),
        )
        assert after_manual is not None
        assert next_activation(after_manual) == (
            NOW + timedelta(minutes=4),
            "schedule",
        )

        store.claim(
            job=job,
            trigger="schedule",
            run_id="run_last",
            now=NOW + timedelta(minutes=4, seconds=30),
        )
        exhausted = store.finish_run(
            jobs={job.id: job},
            run_id="run_last",
            run_status="canceled",
            now=NOW + timedelta(minutes=4, seconds=40),
        )
        assert exhausted is not None and exhausted.status == "done"
        assert next_activation(exhausted) is None
    finally:
        store.close()


def test_chore_body_edit_preserves_cursor_and_schedule_edit_resets_it(
    tmp_path,
) -> None:
    store = JobStore(tmp_path / "jobs.db")
    first = _chore()
    body_edit = replace(first, body="Maintain two.", revision="two")
    schedule_edit = replace(
        body_edit,
        schedule="FREQ=HOURLY;COUNT=2",
    )
    try:
        (initial,) = store.reconcile(jobs={first.id: first}, now=NOW)
        (body_changed,) = store.reconcile(
            jobs={body_edit.id: body_edit},
            now=NOW + timedelta(seconds=10),
        )
        assert body_changed.schedule_anchor == initial.schedule_anchor
        assert body_changed.next_run_at == initial.next_run_at
        assert body_changed.ready_at is None

        changed_at = NOW + timedelta(seconds=20)
        (schedule_changed,) = store.reconcile(
            jobs={schedule_edit.id: schedule_edit},
            now=changed_at,
        )
        assert schedule_changed.schedule_anchor == changed_at.isoformat()
        assert schedule_changed.next_run_at == changed_at.isoformat()
        assert schedule_changed.schedule_revision != initial.schedule_revision
    finally:
        store.close()
