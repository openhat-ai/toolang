from __future__ import annotations

from toolang import work
from toolang.ids import LOCAL_ID_FAMILY, decode_id


def test_task_defaults_to_active_todo_and_stays_active_after_finish(tmp_path) -> None:
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
    finished_path = work.finish_task(
        toolang_root,
        "alice",
        task_id,
        succeeded=True,
    )

    assert finished_path == path
    assert path.exists()
    finished = work.list_tasks(toolang_root, "alice")[0]
    assert finished.document.state == "active"
    assert finished.document.stage == "done"
    assert work.find_archived_task(toolang_root, "alice", task_id) is None


def test_task_remote_stage_maps_status_lines() -> None:
    assert work.TaskFile(body="Status: Todo").remote_stage() == "todo"
    assert work.TaskFile(body="Status: Backlog").remote_stage() == "todo"
    assert work.TaskFile(body="Remote Status: Done").remote_stage() == "done"
    assert work.TaskFile(body="Status: Canceled").remote_stage() == "failed"
    assert work.TaskFile(body="No remote status").remote_stage() is None


def test_task_remote_ref_extracts_issue_key_from_title_or_body() -> None:
    assert work.TaskFile(title="XBY-26 - test").remote_ref() == "XBY-26"
    assert (
        work.TaskFile(body="Link: https://linear.app/xby/issue/XBY-35/example").remote_ref()
        == "XBY-35"
    )
    assert work.TaskFile(title="Review plan", body="No remote link").remote_ref() is None


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
