from __future__ import annotations

from toolang.catalog.job import JobCatalog
import toolang.work.definitions as job_definitions
from toolang.common.ids import LOCAL_ID_FAMILY, decode_id


def test_task_definition_has_id_and_no_runtime_fields(tmp_path) -> None:
    toolang_root = tmp_path / "toolang"
    catalog = JobCatalog(toolang_root, "alice")

    path = catalog.create(
        "task",
        "---\n---\n\nReview the API changes.\n",
    )

    saved = path.read_text(encoding="utf-8")
    assert "\nid: " in saved
    assert "state: active" not in saved
    assert "stage: todo" not in saved

    task = catalog.list(kind="task")[0]
    task_id = task.document.task_id()
    assert path == toolang_root / "agents" / "alice" / "tasks" / f"{task_id}.md"
    assert task.lifecycle == "ready"


def test_task_remote_status_reads_status_lines() -> None:
    assert job_definitions.TaskFile(body="Status: Todo").remote_status() == "Todo"
    assert (
        job_definitions.TaskFile(body="Remote Status: Done").remote_status() == "Done"
    )
    assert job_definitions.TaskFile(body="No remote status").remote_status() is None


def test_task_remote_ref_extracts_issue_key_from_title_or_body() -> None:
    assert job_definitions.TaskFile(title="XBY-26 - test").remote_ref() == "XBY-26"
    assert (
        job_definitions.TaskFile(
            body="Link: https://linear.app/xby/issue/XBY-35/example"
        ).remote_ref()
        == "XBY-35"
    )
    assert (
        job_definitions.TaskFile(
            title="Review plan", body="No remote link"
        ).remote_ref()
        is None
    )


def test_chore_persists_schedule_without_state(tmp_path) -> None:
    toolang_root = tmp_path / "toolang"
    catalog = JobCatalog(toolang_root, "alice")

    path = catalog.create(
        "chore",
        "---\nschedule: FREQ=HOURLY;INTERVAL=2\nstate: inactive\n---\n\nSync state.\n",
    )

    chore = catalog.list(kind="chore")[0]
    saved = path.read_text(encoding="utf-8")

    assert chore.lifecycle == "ready"
    assert chore.document.schedule == "FREQ=HOURLY;INTERVAL=2"
    assert "state: inactive" not in saved
    assert "schedule: FREQ=HOURLY;INTERVAL=2" in saved


def test_lifecycle_moves_between_folders(tmp_path) -> None:
    toolang_root = tmp_path / "toolang"
    catalog = JobCatalog(toolang_root, "alice")
    path = catalog.create("task", "---\n---\n\nDraftable.\n")
    task_id = catalog.list(kind="task")[0].document.task_id()

    draft_path = catalog.draft("task", task_id)
    assert (
        draft_path
        == toolang_root / "agents" / "alice" / "drafts" / "tasks" / f"{task_id}.md"
    )
    assert not path.exists()
    assert catalog.list(kind="task") == ()
    assert (
        catalog.list(kind="task", lifecycle="draft")[0].document.task_id()
        == task_id
    )

    ready_path = catalog.ready("task", task_id)
    assert ready_path == path
    archive_path = catalog.archive("task", task_id)
    assert (
        archive_path
        == toolang_root / "agents" / "alice" / "archive" / "tasks" / f"{task_id}.md"
    )


def test_clone_creates_ready_copy_with_new_id(tmp_path) -> None:
    toolang_root = tmp_path / "toolang"
    catalog = JobCatalog(toolang_root, "alice")
    original = catalog.create(
        "task",
        "---\ntitle: Original\n---\n\nReview the original task.\n",
        lifecycle="draft",
    )
    task_id = catalog.list(kind="task", lifecycle="draft")[0].document.task_id()

    clone = catalog.clone("task", task_id)
    cloned = catalog.list(kind="task")[0]

    assert original.exists()
    assert clone == cloned.path
    assert cloned.document.task_id() != task_id
    assert cloned.document.title == "Original"
    assert cloned.document.body == "Review the original task."
    assert cloned.lifecycle == "ready"


def test_manual_task_file_gets_id_on_scan(tmp_path) -> None:
    toolang_root = tmp_path / "toolang"
    catalog = JobCatalog(toolang_root, "alice")
    path = toolang_root / "agents" / "alice" / "tasks" / "manual.md"
    path.parent.mkdir(parents=True)
    path.write_text(
        "---\ntitle: Manual task\n---\n\nReview manually added job_definitions.\n",
        encoding="utf-8",
    )

    entry = catalog.list(kind="task")[0]
    saved = path.read_text(encoding="utf-8")

    assert entry.path == path
    assert entry.document.task_id()
    assert "\nid: " in saved


def _archive_bucket(value: str) -> str:
    return decode_id(value, family=LOCAL_ID_FAMILY).bucket_started_at.strftime(
        "%Y%m%dT%HZ"
    )
