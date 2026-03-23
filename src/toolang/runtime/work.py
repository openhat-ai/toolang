"""Helpers for listing local task, chore, and will documents."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from toolang.concepts.identity import AgentRef
from toolang.concepts.layout import AgentRoom
from toolang.concepts.persisted import (
    ChoreFile,
    PulseItemState,
    PulseState,
    TaskFile,
    WillFile,
)
from toolang.errors import ToolangError

from .api_models import (
    ChoreItem,
    ChorePatchRequest,
    ChorePutRequest,
    TaskItem,
    TaskPatchRequest,
    TaskPutRequest,
    WillItem,
    WillPatchRequest,
    WillPutRequest,
)


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


def patch_task_item(
    room: AgentRoom, task_id: str, request: TaskPatchRequest
) -> TaskItem:
    """Patch one existing local task document and return its runtime view."""

    path = _task_path(room, task_id)
    if not path.exists():
        raise ToolangError(f"Task not found: {task_id}")
    current = TaskFile.load(path)
    body = _patched_body(
        current.body, body=request.body, body_append=request.body_append
    )
    updated = current.model_copy(
        update={
            "title": request.title if request.title is not None else current.title,
            "body": body,
            "status": request.status if request.status is not None else current.status,
            "assignee": request.assignee
            if request.assignee is not None
            else current.assignee,
            "thread_id": request.thread_id
            if request.thread_id is not None
            else current.thread_id,
            "thunk": request.thunk if request.thunk is not None else current.thunk,
            "model": request.model if request.model is not None else current.model,
            "paused": request.paused if request.paused is not None else current.paused,
        }
    )
    TaskFile.model_validate(updated.model_dump(mode="python")).save(path)
    return _task_item(room, path, TaskFile.load(path), _load_pulse_state(room))


def put_chore_item(
    room: AgentRoom, chore_id: str, request: ChorePutRequest
) -> ChoreItem:
    """Write one full local chore document and return its runtime view."""

    path = _chore_path(room, chore_id)
    ChoreFile(
        title=request.title,
        body=request.body,
        thread_id=request.thread_id,
        interval_sec=request.interval_sec,
        thunk=request.thunk,
        model=request.model,
        paused=request.paused,
    ).save(path)
    return _chore_item(room, path, ChoreFile.load(path), _load_pulse_state(room))


def patch_chore_item(
    room: AgentRoom, chore_id: str, request: ChorePatchRequest
) -> ChoreItem:
    """Patch one existing local chore document and return its runtime view."""

    path = _chore_path(room, chore_id)
    if not path.exists():
        raise ToolangError(f"Chore not found: {chore_id}")
    current = ChoreFile.load(path)
    updated = current.model_copy(
        update={
            "title": request.title if request.title is not None else current.title,
            "body": _patched_body(
                current.body, body=request.body, body_append=request.body_append
            ),
            "thread_id": request.thread_id
            if request.thread_id is not None
            else current.thread_id,
            "interval_sec": (
                request.interval_sec
                if request.interval_sec is not None
                else current.interval_sec
            ),
            "thunk": request.thunk if request.thunk is not None else current.thunk,
            "model": request.model if request.model is not None else current.model,
            "paused": request.paused if request.paused is not None else current.paused,
        }
    )
    ChoreFile.model_validate(updated.model_dump(mode="python")).save(path)
    return _chore_item(room, path, ChoreFile.load(path), _load_pulse_state(room))


def list_chore_items(room: AgentRoom) -> list[ChoreItem]:
    """Return local chore documents under one agent room."""

    pulse_state = _load_pulse_state(room)
    return [
        _chore_item(room, path, document, pulse_state)
        for path, document in _load_markdown_documents(room.chores_dir, ChoreFile.load)
    ]


def put_will_item(
    room: AgentRoom, request: WillPutRequest, *, agent: AgentRef
) -> WillItem:
    """Write one full local will document and return its runtime view."""

    path = room.will_path
    WillFile(
        title=request.title,
        body=request.body,
        thread_id=request.thread_id,
        interval_sec=request.interval_sec,
        thunk=request.thunk,
        model=request.model,
        paused=request.paused,
    ).save(path)
    return _will_item(
        room, path, WillFile.load(path), _load_pulse_state(room), agent=agent
    )


def patch_will_item(
    room: AgentRoom, request: WillPatchRequest, *, agent: AgentRef
) -> WillItem:
    """Patch one existing local will document and return its runtime view."""

    path = room.will_path
    if not path.exists():
        raise ToolangError("Will not found.")
    current = WillFile.load(path)
    updated = current.model_copy(
        update={
            "title": request.title if request.title is not None else current.title,
            "body": _patched_body(
                current.body, body=request.body, body_append=request.body_append
            ),
            "thread_id": request.thread_id
            if request.thread_id is not None
            else current.thread_id,
            "interval_sec": (
                request.interval_sec
                if request.interval_sec is not None
                else current.interval_sec
            ),
            "thunk": request.thunk if request.thunk is not None else current.thunk,
            "model": request.model if request.model is not None else current.model,
            "paused": request.paused if request.paused is not None else current.paused,
        }
    )
    WillFile.model_validate(updated.model_dump(mode="python")).save(path)
    return _will_item(
        room, path, WillFile.load(path), _load_pulse_state(room), agent=agent
    )


def load_will_item(room: AgentRoom, *, agent: AgentRef) -> WillItem | None:
    """Return the local will document for one agent room, if present."""

    path = room.will_path
    if not path.exists():
        return None
    return _will_item(
        room, path, WillFile.load(path), _load_pulse_state(room), agent=agent
    )


def _load_markdown_documents(root: Path, loader):
    if not root.exists():
        return []
    return [
        (path, loader(path)) for path in sorted(root.rglob("*.md")) if path.is_file()
    ]


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


def _patched_body(
    current_body: str, *, body: str | None, body_append: str | None
) -> str:
    if body is not None:
        return body
    if body_append is None:
        return current_body
    suffix = body_append.strip()
    if not suffix:
        return current_body
    return f"{current_body.rstrip()}\n\n{suffix}" if current_body.strip() else suffix


def _task_item(
    room: AgentRoom, path: Path, document: TaskFile, pulse_state: PulseState
) -> TaskItem:
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


def _chore_item(
    room: AgentRoom, path: Path, document: ChoreFile, pulse_state: PulseState
) -> ChoreItem:
    key = _work_key(room.chores_dir, path)
    state = pulse_state.chores.get(key, PulseItemState())
    return ChoreItem(
        id=key,
        title=document.title,
        thread_id=document.effective_thread_id(f"chore:{key}"),
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


def _will_item(
    room: AgentRoom,
    path: Path,
    document: WillFile,
    pulse_state: PulseState,
    *,
    agent: AgentRef,
) -> WillItem:
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


def _task_path(room: AgentRoom, task_id: str) -> Path:
    return _work_path(room.tasks_dir, task_id, label="Task")


def _chore_path(room: AgentRoom, chore_id: str) -> Path:
    return _work_path(room.chores_dir, chore_id, label="Chore")


def _work_path(root: Path, work_id: str, *, label: str) -> Path:
    normalized = Path(work_id)
    if normalized.is_absolute():
        raise ToolangError(f"{label} id may not be an absolute path.")
    parts = [part for part in normalized.parts if part not in {"", "."}]
    if not parts or any(part == ".." for part in parts):
        raise ToolangError(f"Invalid {label.lower()} id: {work_id}")
    return root.joinpath(*parts).with_suffix(".md")
