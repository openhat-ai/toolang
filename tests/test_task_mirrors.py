from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from toolang.concepts.layout import AgentHome, ToolangRoot
from toolang.concepts.persisted import (
    TaskFile,
    TaskMirrorBatch,
    TaskMirrorEntry,
    TaskMirrorSpec,
    TaskMirrorState,
)
from toolang.runtime.work import list_task_items, materialize_task_mirrors


def resolve_toolang_root(root: Path) -> Path:
    return ToolangRoot.resolve(root).path


def test_agent_room_exposes_task_mirrors_path(tmp_path: Path) -> None:
    root = resolve_toolang_root(tmp_path / "toolang-root")
    home = root / "agents" / "alice"
    room = AgentHome.resolve(home).room("alice")

    assert room.task_mirrors_path == home / ".toolang" / "agents" / "alice" / "task_mirrors.json"


def test_task_mirror_state_saves_and_matches_remote_refs(tmp_path: Path) -> None:
    path = tmp_path / "task_mirrors.json"
    now = datetime(2026, 3, 23, 12, 0, tzinfo=timezone.utc)
    state = TaskMirrorState()

    state = state.upsert(
        TaskMirrorEntry(
            provider="linear",
            remote_ref="ISSUE-42",
            local_task_id="a7k2m9xq",
            path="tasks/regression-triage.md",
            remote_updated_at=now,
            last_synced_at=now,
        )
    )
    state = state.upsert(
        TaskMirrorEntry(
            provider="github",
            remote_ref="issues/17",
            local_task_id="b4n8t2wd",
            path="tasks/bugfix.md",
        )
    )
    state.save(path)

    loaded = TaskMirrorState.load(path)

    assert loaded.find(provider="linear", remote_ref="ISSUE-42") == TaskMirrorEntry(
        provider="linear",
        remote_ref="ISSUE-42",
        local_task_id="a7k2m9xq",
        path="tasks/regression-triage.md",
        remote_updated_at=now,
        last_synced_at=now,
    )
    assert loaded.find_by_local_task_id("b4n8t2wd") == TaskMirrorEntry(
        provider="github",
        remote_ref="issues/17",
        local_task_id="b4n8t2wd",
        path="tasks/bugfix.md",
        remote_updated_at=None,
        last_synced_at=None,
    )


def test_task_mirror_state_upsert_replaces_existing_provider_ref() -> None:
    state = TaskMirrorState(
        entries=[
            TaskMirrorEntry(
                provider="linear",
                remote_ref="ISSUE-42",
                local_task_id="a7k2m9xq",
                path="tasks/old.md",
            )
        ]
    )

    updated = state.upsert(
        TaskMirrorEntry(
            provider="linear",
            remote_ref="ISSUE-42",
            local_task_id="a7k2m9xq",
            path="tasks/new.md",
        )
    )

    assert updated.entries == [
        TaskMirrorEntry(
            provider="linear",
            remote_ref="ISSUE-42",
            local_task_id="a7k2m9xq",
            path="tasks/new.md",
            remote_updated_at=None,
            last_synced_at=None,
        )
    ]


def test_materialize_task_mirrors_creates_local_files_and_runtime_items(
    tmp_path: Path,
) -> None:
    root = resolve_toolang_root(tmp_path / "toolang-root")
    home = root / "agents" / "alice"
    room = AgentHome.resolve(home).room("alice")
    batch = TaskMirrorBatch(
        task_mirrors=[
            TaskMirrorSpec(
                provider="linear",
                remote_ref="ISSUE-42",
                name="Regression triage",
                body="Investigate the regression and report back.",
                status="todo",
            )
        ]
    )

    written = materialize_task_mirrors(room, batch)

    assert written == 1
    items = list_task_items(room)
    assert len(items) == 1
    item = items[0]
    saved_task = TaskFile.load(Path(item.path), persist_id=True)
    mirror_state = TaskMirrorState.load(room.task_mirrors_path)
    mirror_entry = mirror_state.find(provider="linear", remote_ref="ISSUE-42")
    assert mirror_entry is not None
    assert item.id == saved_task.task_id()
    assert item.requester == "service:linear"
    assert item.mirrored is True
    assert item.provider == "linear"
    assert item.remote_ref == "ISSUE-42"
    assert Path(item.path).relative_to(room.tasks_dir).parts[0] == "linear"
    assert mirror_entry == TaskMirrorEntry(
        provider="linear",
        remote_ref="ISSUE-42",
        local_task_id=saved_task.task_id(),
        path=str(Path(item.path).relative_to(room.path)),
        remote_updated_at=None,
        last_synced_at=mirror_entry.last_synced_at,
    )
    assert mirror_entry.last_synced_at is not None
