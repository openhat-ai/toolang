from __future__ import annotations

from toolang import work
from toolang.common.ids import LOCAL_ID_FAMILY, decode_id


def test_task_definition_has_id_and_no_runtime_fields(tmp_path) -> None:
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
    assert task.lifecycle == "ready"


def test_task_remote_status_reads_status_lines() -> None:
    assert work.TaskFile(body="Status: Todo").remote_status() == "Todo"
    assert work.TaskFile(body="Remote Status: Done").remote_status() == "Done"
    assert work.TaskFile(body="No remote status").remote_status() is None


def test_task_remote_ref_extracts_issue_key_from_title_or_body() -> None:
    assert work.TaskFile(title="XBY-26 - test").remote_ref() == "XBY-26"
    assert (
        work.TaskFile(body="Link: https://linear.app/xby/issue/XBY-35/example").remote_ref()
        == "XBY-35"
    )
    assert work.TaskFile(title="Review plan", body="No remote link").remote_ref() is None


def test_chore_persists_schedule_without_state(tmp_path) -> None:
    toolang_root = tmp_path / "toolang"

    path = work.create_chore_text(
        toolang_root,
        "alice",
        "---\nschedule: FREQ=HOURLY;INTERVAL=2\nstate: inactive\n---\n\nSync state.\n",
    )

    chore = work.list_chores(toolang_root, "alice")[0]
    saved = path.read_text(encoding="utf-8")

    assert chore.lifecycle == "ready"
    assert chore.document.schedule == "FREQ=HOURLY;INTERVAL=2"
    assert "state: inactive" not in saved
    assert "schedule: FREQ=HOURLY;INTERVAL=2" in saved


def test_lifecycle_moves_between_folders(tmp_path) -> None:
    toolang_root = tmp_path / "toolang"
    path = work.create_task_text(toolang_root, "alice", "---\n---\n\nDraftable.\n")
    task_id = work.list_tasks(toolang_root, "alice")[0].document.task_id()

    draft_path = work.draft_task(toolang_root, "alice", task_id)
    assert draft_path == toolang_root / "agents" / "alice" / "drafts" / "tasks" / f"{task_id}.md"
    assert not path.exists()
    assert work.list_tasks(toolang_root, "alice") == ()
    assert work.list_draft_tasks(toolang_root, "alice")[0].document.task_id() == task_id

    ready_path = work.ready_task(toolang_root, "alice", task_id)
    assert ready_path == path
    archive_path = work.archive_task(toolang_root, "alice", task_id)
    assert archive_path == toolang_root / "agents" / "alice" / "archive" / "tasks" / f"{task_id}.md"


def test_clone_creates_ready_copy_with_new_id(tmp_path) -> None:
    toolang_root = tmp_path / "toolang"
    original = work.create_task_text(
        toolang_root,
        "alice",
        "---\ntitle: Original\n---\n\nReview the original task.\n",
        lifecycle="draft",
    )
    task_id = work.list_draft_tasks(toolang_root, "alice")[0].document.task_id()

    clone = work.clone_task(toolang_root, "alice", task_id)
    cloned = work.list_tasks(toolang_root, "alice")[0]

    assert original.exists()
    assert clone == cloned.path
    assert cloned.document.task_id() != task_id
    assert cloned.document.title == "Original"
    assert cloned.document.body == "Review the original task."
    assert cloned.lifecycle == "ready"


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
