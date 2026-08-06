from datetime import datetime, timezone
from pathlib import Path

from toolang.base.types.message import Message
from toolang.work.files import FileRequestStore
from toolang.work.inbox import collect_file_submissions
from toolang.work.state import Job
from toolang.work.store import JobStore
from toolang.work.types import FileSnapshot


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_job_claim_persists_preallocated_run_id(tmp_path: Path) -> None:
    definition = Job(
        id="TSK-1",
        kind="task",
        title=None,
        body="",
        schedule=None,
        revision="definition-1",
        source="program",
        path=None,
    )
    jobs = {definition.id: definition}
    store = JobStore(tmp_path / "jobs.db")
    try:
        store.reconcile(jobs=jobs, now=NOW)

        claimed = store.claim(
            job=definition,
            trigger="source",
            run_id="run_executor",
            now=NOW,
        )

        assert claimed is not None
        assert claimed.record.status == "running"
        assert claimed.record.active_run_id == "run_executor"
        assert (
            store.finish_run(
                jobs=jobs,
                run_id="run_executor",
                run_status="finished",
                now=NOW,
            )
            is not None
        )
    finally:
        store.close()


def test_file_claim_binds_executor_assigned_run_id(tmp_path: Path) -> None:
    snapshot = FileSnapshot(
        watch_root=str(tmp_path),
        relative_path="note.txt",
        absolute_path=str(tmp_path / "note.txt"),
        size=4,
        mtime_ns=1,
        fingerprint="file-1",
    )
    store = FileRequestStore(tmp_path / "files.db")
    try:
        claimed = store.claim(snapshot, thread_id="file_note", now=NOW)

        assert claimed is not None
        assert claimed.run_id is None
        bound = store.bind_run(
            request_id=claimed.request_id,
            run_id="run_executor",
            now=NOW,
        )
        assert bound.run_id == "run_executor"
        assert (
            store.finish_run(
                run_id="run_executor",
                run_status="finished",
                now=NOW,
            )
            is not None
        )
    finally:
        store.close()


def test_file_collection_claims_without_preallocating_run_id(
    tmp_path: Path,
) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "note.txt").write_text("hello", encoding="utf-8")
    store = FileRequestStore(tmp_path / "files.db")
    try:
        submissions = collect_file_submissions(
            store,
            inboxes=(inbox,),
            stable_ms=0,
            now=NOW,
        )
    finally:
        store.close()

    assert len(submissions) == 1
    assert submissions[0].record.run_id is None
    assert submissions[0].input == Message.user("hello")
