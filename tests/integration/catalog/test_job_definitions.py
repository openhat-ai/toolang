from __future__ import annotations

from pathlib import Path

import frontmatter

from toolang.catalog.chore import AuthoredChores, ChoreFile
from toolang.common.layout import AgentLayout
from toolang.work.state import load_chores


def _job(
    kind,
    job_id: str,
    body: str,
    *,
    title: str | None = None,
    schedule: str | None = None,
    stage="ready",
) -> ChoreFile:
    meta: dict[str, object] = {"id": job_id}
    if title is not None:
        meta["title"] = title
    if schedule is not None:
        meta["schedule"] = schedule
    content = frontmatter.dumps(frontmatter.Post(body, None, **meta))
    return ChoreFile.parse(content, name=job_id)


def test_job_file_parse_projects_original_source_fields() -> None:
    content = "---\nid: review\ntitle: Review API\n---\nReview carefully.\n"

    job = ChoreFile.parse(content, name="review")

    assert job.path is None
    assert job.content == content
    assert job.meta == {"id": "review", "title": "Review API"}
    assert job.body == "Review carefully."


def test_job_file_keeps_identity_in_meta() -> None:
    task = _job("task", "review", "Review the API.", title="XBY-26 - Review")
    chore = _job(
        "chore",
        "sync",
        "Sync state.",
        schedule="FREQ=HOURLY;INTERVAL=2",
    )

    assert task.meta["id"] == "review"
    assert task.name == "review"
    assert chore.schedule == "FREQ=HOURLY;INTERVAL=2"


def test_authored_jobs_crud_returns_job_files(tmp_path: Path) -> None:
    catalog = AuthoredChores(tmp_path)
    created = catalog.create(_job("task", "review", "Review."))
    updated = catalog.update(created.with_body("Review carefully."))
    removed = catalog.remove("review")

    assert created.path == tmp_path / "chores" / "review.md"
    assert updated.body == "Review carefully."
    assert removed == updated
    assert removed.path is not None and not removed.path.exists()


def test_job_title_does_not_rename_the_file(tmp_path: Path) -> None:
    catalog = AuthoredChores(tmp_path)
    created = catalog.create(_job("task", "review", "Review."))

    updated = catalog.update(created.with_meta({**created.meta, "title": "Renamed"}))

    assert updated.name == "review"
    assert updated.path == tmp_path / "chores" / "review.md"
    assert "title: Renamed" in updated.content


def test_state_reads_manual_chore_without_rewriting_its_definition(
    tmp_path: Path,
) -> None:
    home = tmp_path / "agents" / "alice"
    path = home / "chores" / "manual.md"
    path.parent.mkdir(parents=True)
    path.write_text("---\ntitle: Manual\n---\nReview manually.\n", encoding="utf-8")

    jobs = load_chores(AgentLayout.resident(tmp_path, "alice"))
    job = AuthoredChores(home).list()[0]

    assert job.path == path
    assert "id" not in job.meta
    assert job.name == "manual"
    assert jobs[0].id == job.id
    assert (
        path.read_text(encoding="utf-8")
        == "---\ntitle: Manual\n---\nReview manually.\n"
    )
