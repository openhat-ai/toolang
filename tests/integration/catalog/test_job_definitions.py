from __future__ import annotations

from pathlib import Path

import frontmatter
import pytest

from toolang.catalog.job import AuthoredJobs, JobFile
from toolang.common.layout import AgentLayout
from toolang.work.state import HomeJobs


def _job(
    kind,
    job_id: str,
    body: str,
    *,
    title: str | None = None,
    schedule: str | None = None,
    stage="ready",
) -> JobFile:
    meta: dict[str, object] = {"id": job_id}
    if title is not None:
        meta["title"] = title
    if schedule is not None:
        meta["schedule"] = schedule
    content = frontmatter.dumps(frontmatter.Post(body, None, **meta))
    return JobFile.parse(content, kind=kind, stage=stage)


def test_job_file_parse_projects_original_source_fields() -> None:
    content = "---\nid: review\ntitle: Review API\n---\nReview carefully.\n"

    job = JobFile.parse(content, kind="task", stage="draft")

    assert job.path is None
    assert job.content == content
    assert job.kind == "task"
    assert job.stage == "draft"
    assert job.meta == {"id": "review", "name": "review", "title": "Review API"}
    assert job.body == "Review carefully."


def test_authored_jobs_requires_id_for_catalog_writes(tmp_path: Path) -> None:
    job = JobFile.parse("Review.\n", kind="task", name="manual")

    with pytest.raises(ValueError, match="job id is required"):
        AuthoredJobs(tmp_path).create(job)


def test_job_file_keeps_identity_in_meta() -> None:
    task = _job("task", "review", "Review the API.", title="XBY-26 - Review")
    chore = _job(
        "chore",
        "sync",
        "Sync state.",
        schedule="FREQ=HOURLY;INTERVAL=2",
    )

    assert task.meta["id"] == "review"
    assert task.id == "review"
    assert chore.schedule == "FREQ=HOURLY;INTERVAL=2"


def test_authored_jobs_crud_returns_job_files(tmp_path: Path) -> None:
    catalog = AuthoredJobs(tmp_path)
    created = catalog.create(_job("task", "review", "Review."))
    updated = catalog.update(created.with_body("Review carefully."))
    removed = catalog.remove("task", "review")

    assert created.path == tmp_path / "tasks" / "review.md"
    assert updated.body == "Review carefully."
    assert removed == updated
    assert removed.path is not None and not removed.path.exists()


def test_job_name_is_meta_and_does_not_rename_the_file(tmp_path: Path) -> None:
    catalog = AuthoredJobs(tmp_path)
    created = catalog.create(_job("task", "review", "Review."))

    updated = catalog.update(created.with_meta({**created.meta, "name": "renamed"}))

    assert updated.id == "review"
    assert updated.name == "renamed"
    assert updated.path == tmp_path / "tasks" / "review.md"
    assert "name: renamed" in updated.content


def test_state_assigns_and_persists_missing_manual_job_id(tmp_path: Path) -> None:
    home = tmp_path / "agents" / "alice"
    path = home / "tasks" / "manual.md"
    path.parent.mkdir(parents=True)
    path.write_text("---\ntitle: Manual\n---\nReview manually.\n", encoding="utf-8")

    jobs = HomeJobs.load(AgentLayout.resident(tmp_path, "alice"))
    job = AuthoredJobs(home).list(kind="task")[0]

    assert job.path == path
    assert isinstance(job.meta["id"], str)
    assert job.id != "manual"
    assert jobs.definitions[0].id == job.id
    assert f"id: {job.id}" in path.read_text(encoding="utf-8")
