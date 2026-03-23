"""Helpers for listing local task, chore, and will documents."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from toolang.concepts.identity import AgentRef
from toolang.concepts.layout import AgentRoom
from toolang.concepts.persisted import ChoreFile, TaskFile, WillFile

from .api_models import ChoreItem, TaskItem, WillItem


def list_task_items(room: AgentRoom) -> list[TaskItem]:
    """Return local task documents under one agent room."""

    return [
        TaskItem(
            id=_work_key(room.tasks_dir, path),
            title=document.title,
            status=document.status,
            assignee=document.assignee,
            thread_id=document.effective_thread_id(f"task:{_work_key(room.tasks_dir, path)}"),
            thunk=document.thunk,
            model=document.model,
            path=str(path),
            updated_at=_updated_at(path),
            paused=document.paused,
        )
        for path, document in _load_markdown_documents(room.tasks_dir, TaskFile.load)
    ]


def list_chore_items(room: AgentRoom) -> list[ChoreItem]:
    """Return local chore documents under one agent room."""

    return [
        ChoreItem(
            id=_work_key(room.chores_dir, path),
            title=document.title,
            thread_id=document.effective_thread_id(f"chore:{_work_key(room.chores_dir, path)}"),
            interval_sec=document.interval_sec,
            thunk=document.thunk,
            model=document.model,
            path=str(path),
            updated_at=_updated_at(path),
            paused=document.paused,
        )
        for path, document in _load_markdown_documents(room.chores_dir, ChoreFile.load)
    ]


def load_will_item(room: AgentRoom, *, agent: AgentRef) -> WillItem | None:
    """Return the local will document for one agent room, if present."""

    path = room.will_path
    if not path.exists():
        return None
    document = WillFile.load(path)
    return WillItem(
        title=document.title,
        thread_id=document.effective_thread_id(f"will:{agent.id}"),
        interval_sec=document.interval_sec,
        thunk=document.thunk,
        model=document.model,
        path=str(path),
        updated_at=_updated_at(path),
        paused=document.paused,
    )


def _load_markdown_documents(root: Path, loader):
    if not root.exists():
        return []
    return [(path, loader(path)) for path in sorted(root.rglob("*.md")) if path.is_file()]


def _work_key(root: Path, path: Path) -> str:
    relative = path.relative_to(root)
    parts = list(relative.parts)
    if parts:
        parts[-1] = Path(parts[-1]).stem
    return "/".join(parts)


def _updated_at(path: Path) -> str:
    modified = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    return modified.strftime("%Y-%m-%dT%H:%M:%SZ")
