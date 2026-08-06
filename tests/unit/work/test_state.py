from __future__ import annotations

import frontmatter

from toolang.catalog.job import JobFile
from toolang.work.state import (
    job_display_title,
    job_remote_ref,
    job_remote_status,
    job_thread_id,
)


def _job(kind, job_id: str, body: str, *, title: str | None = None) -> JobFile:
    meta = {"id": job_id}
    if title is not None:
        meta["title"] = title
    return JobFile.parse(
        frontmatter.dumps(frontmatter.Post(body, None, **meta)),
        kind=kind,
    )


def test_task_caller_projection_is_owned_by_work() -> None:
    job = _job(
        "task",
        "review",
        "Remote Status: Todo\n\nReview the API.",
        title="XBY-26 - Review",
    )

    assert job_thread_id(job) == "task_review"
    assert job_display_title(job, fallback="review") == "XBY-26 - Review"
    assert job_remote_ref(job) == "XBY-26"
    assert job_remote_status(job) == "Todo"
