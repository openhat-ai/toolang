from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import threading

import frontmatter
import pytest

from toolang.catalog.job import AuthoredJobs, JobFile
from toolang.catalog.errors import DuplicateJobIdError
from toolang.common.layout import AgentLayout
from toolang.lang.ast import Program
from toolang.work.state import AgentJobs, HomeJobs, JobDefinition
from toolang.work.store import JobStore
from toolang.work.watcher import JobWatcher


def _job(kind, job_id: str, body: str, *, stage="ready") -> JobFile:
    content = frontmatter.dumps(frontmatter.Post(body, None, id=job_id))
    return JobFile.parse(content, kind=kind, stage=stage)


def test_job_watcher_current_returns_published_snapshot_without_rescanning(
    tmp_path: Path, monkeypatch
) -> None:
    watcher = JobWatcher(
        AgentLayout.resident(tmp_path / "toolang", "alice")
    )
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
    AuthoredJobs(home).create(_job("task", "review", "Review from file."))
    program = Program.from_source("task review:\n  Review from program.\n")

    jobs = AgentJobs.merge(
        HomeJobs.load(AgentLayout.resident(root, "alice")),
        program,
    )

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

    with pytest.raises(ValueError, match="duplicate home job id: review"):
        AgentJobs.merge(HomeJobs((job, job)), Program.from_source(""))


def test_authored_jobs_moves_between_stages(tmp_path: Path) -> None:
    catalog = AuthoredJobs(tmp_path / "agents" / "alice")
    created = catalog.create(_job("task", "review", "Review this."))

    archived = catalog.move("task", "review", "archived")
    ready = catalog.move("task", "review", "ready")

    assert created.path is not None and ready.path == created.path
    assert archived.stage == "archived"
    assert catalog.get("task", "review") == ready


def test_authored_jobs_preserves_id_and_filename_between_stages(tmp_path: Path) -> None:
    home = tmp_path / "agents" / "alice"
    path = home / "tasks" / "manual-name.md"
    path.parent.mkdir(parents=True)
    path.write_text("---\nid: stable-id\n---\nReview.\n", encoding="utf-8")
    catalog = AuthoredJobs(home)

    archived = catalog.move("task", "stable-id", "archived")

    assert archived.id == "stable-id"
    assert archived.path == home / "archive" / "tasks" / "manual-name.md"
    assert "id: stable-id" in archived.content


def test_authored_jobs_reports_last_modified_duplicate_id(tmp_path: Path) -> None:
    home = tmp_path / "agents" / "alice"
    older = home / "tasks" / "older.md"
    newer = home / "drafts" / "chores" / "newer.md"
    for path in (older, newer):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("---\nid: duplicate\n---\nRun.\n", encoding="utf-8")
    os.utime(older, ns=(1_000_000_000, 1_000_000_000))
    os.utime(newer, ns=(2_000_000_000, 2_000_000_000))

    with pytest.raises(DuplicateJobIdError) as captured:
        AuthoredJobs(home).list()

    assert captured.value.path == newer
    assert captured.value.existing_path == older


def test_job_store_claims_once_across_connections(tmp_path: Path) -> None:
    root = tmp_path / "toolang"
    AuthoredJobs(root / "agents" / "alice").create(
        _job("task", "review", "Review this.")
    )
    jobs = AgentJobs.merge(
        HomeJobs.load(AgentLayout.resident(root, "alice")),
        Program.from_source(""),
    )
    path = root / "agents" / "alice" / ".runtime" / "jobs.db"
    stores = (JobStore(path), JobStore(path))
    for store in stores:
        store.reconcile(jobs=jobs)
    barrier = threading.Barrier(3)
    claims = []
    lock = threading.Lock()

    def claim(store: JobStore) -> None:
        barrier.wait()
        claimed = store.claim_due(
            jobs=jobs,
            kind="task",
            now=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        with lock:
            claims.append(claimed)

    threads = [
        threading.Thread(target=claim, args=(store,))
        for store in stores
    ]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()
    for store in stores:
        store.close()

    assert len([claim for claim in claims if claim is not None]) == 1
