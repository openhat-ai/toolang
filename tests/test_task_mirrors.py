from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from toolang.concepts.layout import AgentHome, ToolangRoot
from toolang.concepts.persisted import TaskMirrorEntry, TaskMirrorState


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
