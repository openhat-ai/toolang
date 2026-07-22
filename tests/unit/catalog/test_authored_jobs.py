"""Tests for authored task and chore files."""

from __future__ import annotations

from datetime import date
import os
from pathlib import Path
from typing import cast

import pytest

from toolang.catalog.errors import (
    CatalogConflictError,
    CatalogNotFoundError,
    DuplicateJobIdError,
)
from toolang.catalog.job import (
    DEFAULT_CHORE_SCHEDULE,
    AuthoredJobs,
    JobFile,
    JobKind,
    JobStage,
)


def _job(
    job_id: str,
    *,
    kind: JobKind = "task",
    name: str | None = None,
    stage: JobStage = "ready",
    body: str = "Do the work.",
    schedule: str | None = None,
) -> JobFile:
    effective_name = name or job_id
    lines = ["---", f"id: {job_id}", f"name: {effective_name}"]
    if schedule is not None:
        lines.append(f"schedule: {schedule}")
    content = "\n".join((*lines, "---", body, ""))
    return JobFile.parse(content, kind=kind, stage=stage)


def _write_job(
    home: Path,
    relative_path: str,
    *,
    job_id: str | None,
    name: str,
    body: str = "Do the work.",
) -> Path:
    path = home / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    id_line = f"id: {job_id}\n" if job_id is not None else ""
    path.write_text(
        f"---\n{id_line}name: {name}\n---\n{body}\n",
        encoding="utf-8",
    )
    return path


def test_job_file_parse_preserves_source_and_projects_fields(tmp_path: Path) -> None:
    path = tmp_path / "tasks" / "review.md"
    content = (
        "---\n"
        "id: task-1\n"
        "name: review\n"
        "title: Review change\n"
        "due: 2026-07-20\n"
        "labels: [backend, urgent]\n"
        "---\n"
        "Review the implementation.\n"
    )

    job = JobFile.parse(content, kind="task", path=path)

    assert job.path == path
    assert job.content == content
    assert job.kind == "task"
    assert job.stage == "ready"
    assert job.id == "task-1"
    assert job.name == "review"
    assert job.title == "Review change"
    assert job.meta == {
        "id": "task-1",
        "name": "review",
        "title": "Review change",
        "due": "2026-07-20",
        "labels": ["backend", "urgent"],
    }
    assert job.body == "Review the implementation."


def test_job_file_parse_projects_caller_identity_without_rewriting_content() -> None:
    content = "---\ntitle: Review change\n---\nReview it.\n"

    job = JobFile.parse(
        content,
        kind="task",
        stage="draft",
        job_id="task-1",
        name="review",
    )

    assert job.content == content
    assert job.id == "task-1"
    assert job.name == "review"
    assert job.stage == "draft"


def test_job_file_requires_name_and_required_id_access() -> None:
    with pytest.raises(ValueError, match="job name is required"):
        JobFile.parse("No metadata.\n", kind="task")

    job = JobFile.parse("Body.\n", kind="task", name="manual")
    assert job.optional_id is None
    with pytest.raises(ValueError, match="job id is required"):
        _ = job.id


def test_job_file_rejects_mismatched_expected_id() -> None:
    with pytest.raises(ValueError, match="does not match the expected id"):
        JobFile.parse(
            "---\nid: authored\nname: review\n---\nBody.\n",
            kind="task",
            job_id="expected",
        )


def test_job_file_updates_meta_and_body_as_authored_content() -> None:
    job = _job("task-1", name="review")

    with_meta = job.with_meta(
        {
            **job.meta,
            "title": "Updated title",
            "due": date(2026, 7, 20),
            "labels": ("backend", "urgent"),
            "nested": {1: date(2026, 7, 21)},
        }
    )
    with_body = with_meta.with_body("Updated body.")

    assert with_meta.meta["due"] == "2026-07-20"
    assert with_meta.meta["labels"] == ["backend", "urgent"]
    assert with_meta.meta["nested"] == {"1": "2026-07-21"}
    assert "title: Updated title" in with_meta.content
    assert with_body.body == "Updated body."
    assert with_body.meta == with_meta.meta
    assert with_body.content.endswith("Updated body.")


def test_job_file_chore_schedule_defaults_and_validates() -> None:
    chore = _job("chore-1", kind="chore")

    assert chore.schedule == DEFAULT_CHORE_SCHEDULE
    with pytest.raises(ValueError, match="only chores have a schedule"):
        _ = _job("task-1").schedule
    with pytest.raises(ValueError):
        _job("chore-2", kind="chore", schedule="not-an-rrule")


def test_authored_jobs_crud_across_stages(tmp_path: Path) -> None:
    catalog = AuthoredJobs(tmp_path)
    task = catalog.create(_job("task-1", name="review"))
    chore = catalog.create(
        _job(
            "chore-1",
            kind="chore",
            name="cleanup",
            stage="draft",
            schedule="FREQ=DAILY",
        )
    )

    assert task.path == tmp_path / "tasks" / "review.md"
    assert chore.path == tmp_path / "drafts" / "chores" / "cleanup.md"
    assert catalog.list(kind="task") == (task,)
    assert catalog.list(kind="chore", stage="draft") == (chore,)
    assert catalog.get("chore", "chore-1") is None
    assert catalog.get("chore", "chore-1", stage=None) == chore
    assert catalog.contains_id("task-1")

    updated = catalog.update(task.with_body("Updated task."))
    moved = catalog.move("task", "task-1", "archived")
    removed = catalog.remove("task", "task-1")

    assert updated.path == task.path
    assert updated.body == "Updated task."
    assert moved.id == task.id
    assert moved.name == task.name
    assert moved.stage == "archived"
    assert moved.path == tmp_path / "archive" / "tasks" / "review.md"
    assert removed == moved
    assert removed.path is not None and not removed.path.exists()
    assert catalog.contains_id("task-1") is False


def test_authored_jobs_create_rejects_duplicate_id_and_name(tmp_path: Path) -> None:
    catalog = AuthoredJobs(tmp_path)
    catalog.create(_job("task-1", name="review"))

    with pytest.raises(CatalogConflictError, match="job id already exists"):
        catalog.create(_job("task-1", kind="chore", name="cleanup", stage="draft"))
    with pytest.raises(CatalogConflictError, match="job name already exists"):
        catalog.create(_job("task-2", name="review"))


def test_authored_jobs_update_rejects_missing_kind_and_stage_changes(
    tmp_path: Path,
) -> None:
    catalog = AuthoredJobs(tmp_path)
    task = catalog.create(_job("task-1", name="review"))

    with pytest.raises(CatalogNotFoundError, match="job not found"):
        catalog.update(_job("missing", name="missing"))
    with pytest.raises(CatalogConflictError, match="belongs to task"):
        catalog.update(_job("task-1", kind="chore", name="review"))
    with pytest.raises(ValueError, match="use move"):
        catalog.update(_job("task-1", name="review", stage="draft"))

    assert catalog.get("task", task.id) == task


def test_authored_jobs_move_is_idempotent_and_rejects_target_conflict(
    tmp_path: Path,
) -> None:
    catalog = AuthoredJobs(tmp_path)
    task = catalog.create(_job("task-1", name="review"))

    assert catalog.move("task", task.id, "ready") == task
    catalog.create(_job("task-2", name="review", stage="draft"))
    with pytest.raises(CatalogConflictError, match="job name already exists"):
        catalog.move("task", task.id, "draft")
    with pytest.raises(CatalogNotFoundError, match="not found"):
        catalog.move("task", "missing", "archived")


def test_authored_jobs_remove_rejects_missing_job(tmp_path: Path) -> None:
    with pytest.raises(CatalogNotFoundError, match="not found"):
        AuthoredJobs(tmp_path).remove("task", "missing")


def test_authored_jobs_assigns_and_persists_missing_ids(tmp_path: Path) -> None:
    _write_job(tmp_path, "tasks/review.md", job_id=None, name="review")
    _write_job(
        tmp_path,
        "drafts/chores/cleanup.md",
        job_id=None,
        name="cleanup",
    )
    _write_job(tmp_path, "archive/tasks/done.md", job_id="existing", name="done")
    allocated = iter(("generated-1", "generated-2"))
    catalog = AuthoredJobs(tmp_path)

    assigned = catalog.assign_missing_ids(lambda: next(allocated))

    assert [job.id for job in assigned] == ["generated-1", "generated-2"]
    assert catalog.get("task", "generated-2", stage=None) is not None
    assert catalog.get("chore", "generated-1", stage=None) is not None
    assert catalog.get("task", "existing", stage=None) is not None
    assert "id: generated-2" in (
        tmp_path / "tasks" / "review.md"
    ).read_text(encoding="utf-8")


def test_authored_jobs_rejects_generated_id_collision(tmp_path: Path) -> None:
    _write_job(tmp_path, "tasks/existing.md", job_id="task-1", name="existing")
    _write_job(tmp_path, "tasks/manual.md", job_id=None, name="manual")

    with pytest.raises(CatalogConflictError, match="generated job id already exists"):
        AuthoredJobs(tmp_path).assign_missing_ids(lambda: "task-1")


def test_authored_jobs_reports_latest_duplicate_id_file(tmp_path: Path) -> None:
    existing = _write_job(
        tmp_path,
        "tasks/review.md",
        job_id="duplicate",
        name="review",
    )
    latest = _write_job(
        tmp_path,
        "drafts/chores/cleanup.md",
        job_id="duplicate",
        name="cleanup",
    )
    os.utime(existing, ns=(1_000_000_000, 1_000_000_000))
    os.utime(latest, ns=(2_000_000_000, 2_000_000_000))

    with pytest.raises(DuplicateJobIdError) as raised:
        AuthoredJobs(tmp_path).list()

    assert raised.value.job_id == "duplicate"
    assert raised.value.path == latest
    assert raised.value.existing_path == existing


@pytest.mark.parametrize(
    ("kind", "stage", "job_id", "name", "message"),
    [
        ("note", "ready", "task-1", "review", "unsupported job kind"),
        ("task", "pending", "task-1", "review", "unsupported job stage"),
        ("task", "ready", "../task", "review", "invalid job id"),
        ("task", "ready", "task-1", "../review", "invalid job name"),
    ],
)
def test_job_file_rejects_invalid_identity_and_placement(
    kind: str,
    stage: str,
    job_id: str,
    name: str,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        JobFile(
            path=None,
            content="Body.\n",
            kind=cast(JobKind, kind),
            stage=cast(JobStage, stage),
            meta={"id": job_id, "name": name},
            body="Body.",
        )


def test_job_file_rejects_empty_optional_title() -> None:
    with pytest.raises(ValueError, match="title must be non-empty text"):
        JobFile(
            path=None,
            content="Body.\n",
            kind="task",
            stage="ready",
            meta={"id": "task-1", "name": "review", "title": " "},
            body="Body.",
        )
