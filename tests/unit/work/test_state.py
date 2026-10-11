from __future__ import annotations

import frontmatter

from toolang.catalog.chore import ChoreFile
from toolang.work.state import (
    chore_display_title,
    chore_remote_ref,
    chore_remote_status,
    chore_thread_id,
)


def _job(kind, job_id: str, body: str, *, title: str | None = None) -> ChoreFile:
    meta = {"id": job_id}
    if title is not None:
        meta["title"] = title
    return ChoreFile.parse(
        frontmatter.dumps(frontmatter.Post(body, None, **meta)),
        name=job_id,
    )


def test_task_caller_projection_is_owned_by_work() -> None:
    job = _job(
        "task",
        "review",
        "Remote Status: Todo\n\nReview the API.",
        title="XBY-26 - Review",
    )

    assert chore_thread_id(job) == f"chore_{job.id}"
    assert chore_display_title(job, fallback="review") == "XBY-26 - Review"
    assert chore_remote_ref(job) == "XBY-26"
    assert chore_remote_status(job) == "Todo"


def test_name_identity_shadowing_and_collision(tmp_path, monkeypatch):
    import pytest
    from dataclasses import replace
    from toolang.lang.ast import Program
    from toolang.work import state

    program = Program.from_source(
        "chore cleanup:\n  First.\nchore cleanup:\n  Second.\n"
    )
    first, second = state.program_chores(program)
    capitalized = replace(first, name="Cleanup")
    assert first.id == second.id != capitalized.id
    assert len(first.id) == 16
    assert state.merge_chores((first, second))[first.id] == second
    file = ChoreFile.parse("First.", name="cleanup", path=tmp_path / "cleanup.md")
    assert state._file_chore(file).revision == first.revision
    assert file.id == first.id
    monkeypatch.setattr(state, "chore_id", lambda _: "0" * 16)
    with pytest.raises(ValueError, match="collision"):
        state.merge_chores((first, capitalized))
