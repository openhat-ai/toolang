from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import threading

import pytest

from toolang.catalog.job import JobCatalog
from toolang.work.state import AgentJobs, HomeJobs, JobDefinition
from toolang.work.store import JobStore
from toolang.work.watcher import JobWatcher
from toolang.lang.ast import Program
from toolang.work.definitions import TaskFile


def test_job_watcher_current_returns_published_snapshot_without_rescanning(
    tmp_path: Path, monkeypatch
) -> None:
    watcher = JobWatcher(tmp_path / "toolang", "alice")
    published = watcher.current()
    monkeypatch.setattr(
        HomeJobs,
        "load",
        lambda *_args, **_kwargs: pytest.fail("current() must not scan authored jobs"),
    )

    assert watcher.current() is published


def test_agent_jobs_merge_home_over_program(tmp_path: Path) -> None:
    root = tmp_path / "toolang"
    home = root / "agents" / "alice"
    (home / "tasks").mkdir(parents=True)
    (home / "tasks" / "review.md").write_text(
        "---\nid: review\n---\nReview from file.\n",
        encoding="utf-8",
    )
    program = Program.from_source("task review:\n  Review from program.\n")

    jobs = AgentJobs.merge(HomeJobs.load(root, "alice"), program)

    assert len(jobs.definitions) == 1
    assert jobs.definitions[0].input == "Review from file."
    assert jobs.definitions[0].source.endswith("tasks/review.md")


def test_agent_jobs_reject_duplicate_home_ids() -> None:
    job = JobDefinition(
        id="review",
        kind="task",
        name="review",
        title=None,
        body="Review.",
        source="tasks/review.md",
        path="/tmp/review.md",
        input="Review.",
        schedule=None,
        fingerprint="abc",
        thread="task_review",
    )

    with pytest.raises(ValueError, match="duplicate home task id: review"):
        AgentJobs.merge(HomeJobs((job, job)), Program.from_source(""))


def test_job_catalog_moves_authored_lifecycle(tmp_path: Path) -> None:
    catalog = JobCatalog(tmp_path / "toolang", "alice")

    path = catalog.create("task", "---\ntitle: Review\n---\nReview this.\n")
    entry = catalog.list(kind="task")[0]
    assert isinstance(entry.document, TaskFile)
    task_id = entry.document.task_id()

    assert path.is_file()
    assert catalog.archive("task", task_id) is not None
    assert catalog.get("task", task_id, lifecycle="archived") is not None
    assert catalog.reopen("task", task_id) is not None
    assert catalog.get("task", task_id) is not None


def test_job_store_claims_once_across_connections(tmp_path: Path) -> None:
    root = tmp_path / "toolang"
    catalog = JobCatalog(root, "alice")
    catalog.create("task", "---\ntitle: Review\n---\nReview this.\n")
    jobs = AgentJobs.merge(HomeJobs.load(root, "alice"), Program.from_source(""))
    path = root / "agents" / "alice" / ".runtime" / "jobs.db"
    stores = (JobStore(path), JobStore(path))
    for store in stores:
        store.reconcile(jobs=jobs)
    barrier = threading.Barrier(3)
    claims = []
    lock = threading.Lock()

    def claim(store: JobStore, run_id: str) -> None:
        barrier.wait()
        claimed = store.claim_due(
            jobs=jobs,
            kind="task",
            run_id=run_id,
            now=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        with lock:
            claims.append(claimed)

    threads = [
        threading.Thread(target=claim, args=(store, f"run-{index}"))
        for index, store in enumerate(stores)
    ]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()
    for store in stores:
        store.close()

    assert len([claim for claim in claims if claim is not None]) == 1
