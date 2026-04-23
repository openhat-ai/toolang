from __future__ import annotations

from toolang import work
from toolang.ids import LOCAL_ID_FAMILY, decode_id


def test_task_defaults_to_active_todo_and_archives_after_success(tmp_path) -> None:
    toolang_root = tmp_path / "toolang"

    path = work.create_task_text(
        toolang_root,
        "alice",
        "---\n---\n\nReview the API changes.\n",
    )

    saved = path.read_text(encoding="utf-8")
    assert "\nid: " in saved
    assert "state: active" not in saved
    assert "stage: todo" not in saved

    task = work.list_tasks(toolang_root, "alice")[0]
    task_id = task.document.task_id()
    assert path == toolang_root / "agents" / "alice" / "tasks" / f"{task_id}.md"
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
    assert archived.document.stage == "done"
    assert archived.path == archived_path
    assert "archive" in archived_path.parts
    assert "tasks" in archived_path.parts
    assert archived_path.parent.name == _archive_bucket(task_id)


def test_chore_persists_schedule_and_state(tmp_path) -> None:
    toolang_root = tmp_path / "toolang"

    path = work.create_chore_text(
        toolang_root,
        "alice",
        "---\nschedule: FREQ=HOURLY;INTERVAL=2\nstate: inactive\n---\n\nSync state.\n",
    )

    chore = work.list_chores(toolang_root, "alice")[0]
    saved = path.read_text(encoding="utf-8")

    assert chore.document.state == "inactive"
    assert chore.document.schedule == "FREQ=HOURLY;INTERVAL=2"
    assert "state: inactive" in saved
    assert "schedule: FREQ=HOURLY;INTERVAL=2" in saved


def test_manual_task_file_gets_id_on_scan(tmp_path) -> None:
    toolang_root = tmp_path / "toolang"
    path = toolang_root / "agents" / "alice" / "tasks" / "manual.md"
    path.parent.mkdir(parents=True)
    path.write_text("---\ntitle: Manual task\n---\n\nReview manually added work.\n", encoding="utf-8")

    entry = work.list_tasks(toolang_root, "alice")[0]
    saved = path.read_text(encoding="utf-8")

    assert entry.path == path
    assert entry.document.task_id()
    assert "\nid: " in saved


def _archive_bucket(value: str) -> str:
    return decode_id(value, family=LOCAL_ID_FAMILY).bucket_started_at.strftime("%Y%m%dT%HZ")
