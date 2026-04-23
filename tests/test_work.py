from __future__ import annotations

from toolang import work


def test_task_defaults_to_active_todo_and_archives_after_success(tmp_path) -> None:
    toolang_root = tmp_path / "toolang"

    path = work.put_task_text(
        toolang_root,
        "alice",
        "review",
        "---\n---\n\nReview the API changes.\n",
    )

    saved = path.read_text(encoding="utf-8")
    assert "\nid: " in saved
    assert "state: active" not in saved
    assert "status: todo" not in saved

    task = work.list_tasks(toolang_root, "alice")[0]
    task_id = task.document.task_id()
    archived_path = work.finish_task(
        toolang_root,
        "alice",
        task_id,
        succeeded=True,
    )

    assert archived_path is not None
    assert not path.exists()
    assert work.list_tasks(toolang_root, "alice") == ()

    archived = work.list_tasks(toolang_root, "alice", include_archived=True)[0]
    assert archived.document.state == "archived"
    assert archived.document.status == "done"
    assert archived.path == archived_path
    assert "archive" in archived_path.parts
    assert "tasks" in archived_path.parts


def test_chore_normalizes_legacy_rrule_and_paused_state(tmp_path) -> None:
    toolang_root = tmp_path / "toolang"

    path = work.put_chore_text(
        toolang_root,
        "alice",
        "sync",
        "---\nrrule: FREQ=HOURLY;INTERVAL=2\npaused: true\n---\n\nSync state.\n",
    )

    chore = work.list_chores(toolang_root, "alice")[0]
    saved = path.read_text(encoding="utf-8")

    assert chore.document.state == "inactive"
    assert chore.document.schedule == "FREQ=HOURLY;INTERVAL=2"
    assert "state: inactive" in saved
    assert "schedule: FREQ=HOURLY;INTERVAL=2" in saved
    assert "paused:" not in saved
    assert "rrule:" not in saved
