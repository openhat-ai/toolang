from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from toolang.work.state import Chore
from toolang.work.store import ChoreStore, next_activation


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _task(revision: str = "one") -> Chore:
    return Chore(
        name="review",
        title=None,
        body=f"Review {revision}.",
        schedule=None,
        revision=revision,
        source="chores/review.md",
        path=None,
    )


def _chore(
    *,
    revision: str = "one",
    schedule: str = "FREQ=MINUTELY;COUNT=5",
) -> Chore:
    return Chore(
        name="maintain",
        title=None,
        body=f"Maintain {revision}.",
        schedule=schedule,
        revision=revision,
        source="chores/maintain.md",
        path=None,
    )


def test_task_revisions_coalesce_and_run_serially(tmp_path) -> None:
    store = ChoreStore(tmp_path / "runs.db")
    first = _task()
    second = replace(first, body="Review two.", revision="two")
    try:
        (pending,) = store.reconcile(chores={first.id: first}, now=NOW)
        claimed = store.claim(
            chore=first,
            trigger="source",
            run_id="run_one",
            now=NOW,
        )
        assert pending.status == "pending"
        assert claimed.record.status == "running"
        assert next_activation(claimed.record) is None

        (edited,) = store.reconcile(
            chores={second.id: second},
            now=NOW + timedelta(seconds=1),
        )
        assert edited.status == "running"
        assert edited.revision == "two"
        assert edited.active_revision == "one"
        assert edited.ready_at == (NOW + timedelta(seconds=1)).isoformat()

        after_first = store.finish_run(
            chores={second.id: second},
            run_id="run_one",
            run_status="succeeded",
            now=NOW + timedelta(seconds=2),
        )
        assert after_first is not None
        assert after_first.status == "pending"

        store.claim(
            chore=second,
            trigger="source",
            run_id="run_two",
            now=NOW + timedelta(seconds=2),
        )
        done = store.finish_run(
            chores={second.id: second},
            run_id="run_two",
            run_status="succeeded",
            now=NOW + timedelta(seconds=3),
        )
        assert done is not None and done.status == "done"
        assert (
            store.reconcile(
                chores={second.id: second},
                now=NOW + timedelta(seconds=4),
            )[0].status
            == "done"
        )

        reopened = store.request_manual_chore(
            chore_id=second.id,
            now=NOW + timedelta(seconds=5),
        )
        assert reopened.status == "pending"
        assert reopened.ready_at == (NOW + timedelta(seconds=5)).isoformat()
    finally:
        store.close()


def test_active_job_survives_ready_removal_until_run_finishes(tmp_path) -> None:
    store = ChoreStore(tmp_path / "runs.db")
    job = _task()
    try:
        store.reconcile(chores={job.id: job}, now=NOW)
        store.claim(
            chore=job,
            trigger="source",
            run_id="run_one",
            now=NOW,
        )

        retained = store.reconcile(chores={}, now=NOW + timedelta(seconds=1))
        assert len(retained) == 1
        assert retained[0].active_run_id == "run_one"

        assert (
            store.finish_run(
                chores={},
                run_id="run_one",
                run_status="succeeded",
                now=NOW + timedelta(seconds=2),
            )
            is not None
        )
        assert store.list()[0].status == "done"
        assert store.reconcile(chores={job.id: job}, now=NOW)[0].status == "done"
    finally:
        store.close()


def test_rejected_start_preserves_a_newer_pending_task_revision(tmp_path) -> None:
    store = ChoreStore(tmp_path / "runs.db")
    first = _task()
    second = replace(first, body="Review two.", revision="two")
    try:
        store.reconcile(chores={first.id: first}, now=NOW)
        store.claim(
            chore=first,
            trigger="source",
            run_id="run_rejected",
            now=NOW,
        )
        store.reconcile(
            chores={second.id: second},
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
                chores={second.id: second},
                run_id="missing",
                run_status="running",
            )
    finally:
        store.close()


def test_missing_run_releases_the_original_activation(tmp_path) -> None:
    store = ChoreStore(tmp_path / "runs.db")
    job = _task()
    try:
        store.reconcile(chores={job.id: job}, now=NOW)
        claimed = store.claim(
            chore=job,
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
    store = ChoreStore(tmp_path / "runs.db")
    job = _chore()
    try:
        (initial,) = store.reconcile(chores={job.id: job}, now=NOW)
        assert next_activation(initial) == (NOW + timedelta(minutes=1), "schedule")

        claimed = store.claim(
            chore=job,
            trigger="schedule",
            run_id="run_schedule",
            now=NOW + timedelta(minutes=3, seconds=30),
        )
        assert claimed.active_at == (NOW + timedelta(minutes=3)).isoformat()
        assert claimed.record.next_run_at == (NOW + timedelta(minutes=4)).isoformat()

        waiting = store.finish_run(
            chores={job.id: job},
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
            chore=job,
            trigger="manual",
            run_id="run_manual",
            now=NOW + timedelta(minutes=3, seconds=45),
        )
        after_manual = store.finish_run(
            chores={job.id: job},
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
            chore=job,
            trigger="schedule",
            run_id="run_last",
            now=NOW + timedelta(minutes=4, seconds=30),
        )
        exhausted = store.finish_run(
            chores={job.id: job},
            run_id="run_last",
            run_status="canceled",
            now=NOW + timedelta(minutes=4, seconds=40),
        )
        assert exhausted is not None and exhausted.status == "canceled"
        assert next_activation(exhausted) is None
    finally:
        store.close()


def test_chore_body_edit_preserves_cursor_and_schedule_edit_resets_it(
    tmp_path,
) -> None:
    store = ChoreStore(tmp_path / "runs.db")
    first = _chore()
    body_edit = replace(first, body="Maintain two.", revision="two")
    schedule_edit = replace(
        body_edit,
        schedule="FREQ=HOURLY;COUNT=2",
    )
    try:
        (initial,) = store.reconcile(chores={first.id: first}, now=NOW)
        (body_changed,) = store.reconcile(
            chores={body_edit.id: body_edit},
            now=NOW + timedelta(seconds=10),
        )
        assert body_changed.schedule_anchor == initial.schedule_anchor
        assert body_changed.next_run_at == initial.next_run_at
        assert body_changed.ready_at is None

        changed_at = NOW + timedelta(seconds=20)
        (schedule_changed,) = store.reconcile(
            chores={schedule_edit.id: schedule_edit},
            now=changed_at,
        )
        assert schedule_changed.schedule_anchor == changed_at.isoformat()
        assert (
            schedule_changed.next_run_at
            == (changed_at + timedelta(hours=1)).isoformat()
        )
        assert schedule_changed.schedule_revision != initial.schedule_revision
    finally:
        store.close()


def test_chore_checkpoints_share_runs_database_without_changing_execution_schema(
    tmp_path,
) -> None:
    import sqlite3
    from toolang.execution.store import RunStore

    path = tmp_path / "runs.db"
    runs = RunStore(path)
    runs.create_thread(thread_id="script_existing", origin="script")
    with sqlite3.connect(path) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
    chores = ChoreStore(path)
    try:
        definition = _task()
        chores.reconcile(chores=(definition,), now=NOW)
        assert runs.get_thread(thread_id="script_existing") is not None
        with sqlite3.connect(path) as connection:
            assert connection.execute("PRAGMA user_version").fetchone()[0] == version
            assert connection.execute("SELECT name FROM chores").fetchone() == (
                definition.name,
            )
    finally:
        chores.close()
        runs.close()


@pytest.mark.parametrize(
    "outcome,expected",
    [("succeeded", "done"), ("failed", "failed"), ("canceled", "canceled")],
)
def test_removed_one_off_retains_terminal_revision_across_restart(
    tmp_path, outcome, expected
):
    path = tmp_path / "runs.db"
    definition = _task()
    store = ChoreStore(path)
    try:
        store.reconcile(chores=(definition,), now=NOW)
        store.claim(chore=definition, trigger="source", run_id="run_once", now=NOW)
        store.finish_run(chores={}, run_id="run_once", run_status=outcome, now=NOW)
    finally:
        store.close()
    store = ChoreStore(path)
    try:
        assert store.reconcile(chores=(), now=NOW)[0].status == expected
        restored = store.reconcile(chores=(definition,), now=NOW)[0]
        assert restored.status == expected
        assert next_activation(restored) is None
        renamed = replace(definition, name="renamed")
        records = store.reconcile(chores=(renamed,), now=NOW)
        assert (
            next(record for record in records if record.name == "renamed").status
            == "pending"
        )
        assert (
            next(record for record in records if record.name == definition.name).status
            == expected
        )
    finally:
        store.close()
