from datetime import datetime, timezone
from pathlib import Path

from toolang.work.state import Job
from toolang.work.store import JobStore


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
                run_status="succeeded",
                now=NOW,
            )
            is not None
        )
    finally:
        store.close()
