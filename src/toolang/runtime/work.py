"""Helpers for listing local task, chore, and will documents."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from toolang.concepts.identity import AgentRef
from toolang.concepts.layout import AgentRoom
from toolang.concepts.persisted import ChoreFile, PulseItemState, PulseState, TaskFile, WillFile
from toolang.errors import ToolangError

from .api_models import ChoreItem, TaskItem, TaskPatchRequest, TaskPutRequest, WillItem


def list_task_items(room: AgentRoom) -> list[TaskItem]:
    """Return local task documents under one agent room."""

    pulse_state = _load_pulse_state(room)
    return [
        _task_item(room, path, document, pulse_state)
        for path, document in _load_markdown_documents(room.tasks_dir, TaskFile.load)
    ]


def put_task_item(room: AgentRoom, task_id: str, request: TaskPutRequest) -> TaskItem:
    """Write one full local task document and return its runtime view."""

    path = _task_path(room, task_id)
    TaskFile(
        title=request.title,
        body=request.body,
        status=request.status,
        assignee=request.assignee,
        thread_id=request.thread_id,
        thunk=request.thunk,
        model=request.model,
        paused=request.paused,
    ).save(path)
    return _task_item(room, path, TaskFile.load(path), _load_pulse_state(room))


def patch_task_item(room: AgentRoom, task_id: str, request: TaskPatchRequest) -> TaskItem:
    """Patch one existing local task document and return its runtime view."""

    path = _task_path(room, task_id)
    if not path.exists():
        raise ToolangError(f"Task not found: {task_id}")
    current = TaskFile.load(path)
    body = current.body
    if request.body is not None:
        body = request.body
    elif request.body_append is not None:
        suffix = request.body_append.strip()
        if suffix:
            body = f"{body.rstrip()}\n\n{suffix}" if body.strip() else suffix
    updated = current.model_copy(
        update={
            "title": request.title if request.title is not None else current.title,
            "body": body,
            "status": request.status if request.status is not None else current.status,
            "assignee": request.assignee if request.assignee is not None else current.assignee,
            "thread_id": request.thread_id if request.thread_id is not None else current.thread_id,
            "thunk": request.thunk if request.thunk is not None else current.thunk,
            "model": request.model if request.model is not None else current.model,
            "paused": request.paused if request.paused is not None else current.paused,
        }
    )
    TaskFile.model_validate(updated.model_dump(mode="python")).save(path)
    return _task_item(room, path, TaskFile.load(path), _load_pulse_state(room))


def list_chore_items(room: AgentRoom) -> list[ChoreItem]:
    """Return local chore documents under one agent room."""

    pulse_state = _load_pulse_state(room)
    return [
        ChoreItem(
            id=_work_key(room.chores_dir, path),
            title=document.title,
            thread_id=document.effective_thread_id(f"chore:{_work_key(room.chores_dir, path)}"),
            interval_sec=document.interval_sec,
            thunk=document.thunk,
            model=document.model,
            path=str(path),
            last_enqueued_at=_iso(pulse_state.chores.get(_work_key(room.chores_dir, path), PulseItemState()).last_enqueued_at),
            last_started_at=_iso(pulse_state.chores.get(_work_key(room.chores_dir, path), PulseItemState()).last_started_at),
            last_finished_at=_iso(pulse_state.chores.get(_work_key(room.chores_dir, path), PulseItemState()).last_finished_at),
            last_status=pulse_state.chores.get(_work_key(room.chores_dir, path), PulseItemState()).last_status,
            last_run_id=pulse_state.chores.get(_work_key(room.chores_dir, path), PulseItemState()).last_run_id,
            next_due_at=_iso(pulse_state.chores.get(_work_key(room.chores_dir, path), PulseItemState()).next_due_at),
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
    pulse_state = _load_pulse_state(room)
    state = pulse_state.will
    return WillItem(
        title=document.title,
        thread_id=document.effective_thread_id(f"will:{agent.id}"),
        interval_sec=document.interval_sec,
        thunk=document.thunk,
        model=document.model,
        path=str(path),
        last_enqueued_at=_iso(state.last_enqueued_at),
        last_started_at=_iso(state.last_started_at),
        last_finished_at=_iso(state.last_finished_at),
        last_status=state.last_status,
        last_run_id=state.last_run_id,
        next_due_at=_iso(state.next_due_at),
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


def _load_pulse_state(room: AgentRoom) -> PulseState:
    path = room.pulse_state_path
    if not path.exists():
        return PulseState()
    return PulseState.load(path)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _task_item(room: AgentRoom, path: Path, document: TaskFile, pulse_state: PulseState) -> TaskItem:
    key = _work_key(room.tasks_dir, path)
    state = pulse_state.tasks.get(key, PulseItemState())
    return TaskItem(
        id=key,
        title=document.title,
        status=document.status,
        assignee=document.assignee,
        thread_id=document.effective_thread_id(f"task:{key}"),
        thunk=document.thunk,
        model=document.model,
        path=str(path),
        last_enqueued_at=_iso(state.last_enqueued_at),
        last_started_at=_iso(state.last_started_at),
        last_finished_at=_iso(state.last_finished_at),
        last_status=state.last_status,
        last_run_id=state.last_run_id,
        updated_at=_updated_at(path),
        paused=document.paused,
    )


def _task_path(room: AgentRoom, task_id: str) -> Path:
    normalized = Path(task_id)
    if normalized.is_absolute():
        raise ToolangError("Task id may not be an absolute path.")
    parts = [part for part in normalized.parts if part not in {"", "."}]
    if not parts or any(part == ".." for part in parts):
        raise ToolangError(f"Invalid task id: {task_id}")
    return room.tasks_dir.joinpath(*parts).with_suffix(".md")
