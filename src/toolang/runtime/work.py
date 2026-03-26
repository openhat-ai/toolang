"""Helpers for listing local task, chore, and will documents."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import re

from toolang.concepts.identity import AgentRef
from toolang.concepts.layout import AgentRoom
from toolang.concepts.persisted import (
    ChoreFile,
    TaskMirrorBatch,
    TaskMirrorEntry,
    TaskMirrorSpec,
    TaskMirrorState,
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

    mirror_state = _load_task_mirror_state(room)
    return [
        _task_item(room, path, document, mirror_state)
        for path, document in _load_markdown_documents(
            room.tasks_dir,
            lambda value: TaskFile.load(value, persist_id=True),
        )
    ]


def put_task_item(
    room: AgentRoom, task_name: str, request: TaskPutRequest
) -> TaskItem:
    """Write one full local task document and return its runtime view."""

    path = _task_path(room, task_name)
    TaskFile(
        id=request.id,
        body=request.body,
        status=request.status,
        requester=request.requester or "owner",
        paused=request.paused,
    ).save(path)
    return _task_item(
        room,
        path,
        TaskFile.load(path, persist_id=True),
        _load_task_mirror_state(room),
    )


def patch_task_item(
    room: AgentRoom, task_name: str, request: TaskPatchRequest
) -> TaskItem:
    """Patch one existing local task document and return its runtime view."""

    path = _task_path(room, task_name)
    if not path.exists():
        raise ToolangError(f"Task not found: {task_name}")
    current = TaskFile.load(path, persist_id=True)
    body = _patched_body(
        current.body, body=request.body, body_append=request.body_append
    )
    updated = current.model_copy(
        update={
            "body": body,
            "status": request.status if request.status is not None else current.status,
            "requester": request.requester
            if request.requester is not None
            else current.requester,
            "paused": request.paused if request.paused is not None else current.paused,
        }
    )
    TaskFile.model_validate(updated.model_dump(mode="python")).save(path)
    return _task_item(
        room,
        path,
        TaskFile.load(path, persist_id=True),
        _load_task_mirror_state(room),
    )


def materialize_task_mirror_output(room: AgentRoom, output: str) -> int:
    """Write mirrored local task files from one chore output payload."""

    text = output.strip()
    if not text or not text.startswith("{"):
        return 0
    try:
        batch = TaskMirrorBatch.model_validate_json(text)
    except Exception:
        return 0
    if not batch.task_mirrors:
        return 0
    return materialize_task_mirrors(room, batch)


def materialize_task_mirrors(
    room: AgentRoom,
    batch: TaskMirrorBatch,
    *,
    synced_at: datetime | None = None,
) -> int:
    """Create or update local mirrored task files from remote task snapshots."""

    now = synced_at or datetime.now(timezone.utc)
    state = _load_task_mirror_state(room)
    written = 0
    for item in batch.task_mirrors:
        existing = state.find(provider=item.provider, remote_ref=item.remote_ref)
        if existing is None:
            task = TaskFile(
                requester=f"service:{item.provider}",
                status=item.status,
                body=item.body,
            ).with_id()
            task_id = task.task_id()
            path = _new_mirror_task_path(room, item, task_id)
        else:
            task_id = existing.local_task_id
            path = _mirror_task_path(room, existing)
            current = TaskFile.load(path, persist_id=True) if path.exists() else TaskFile(id=task_id)
            task = current.model_copy(
                update={
                    "id": task_id,
                    "requester": f"service:{item.provider}",
                    "status": item.status,
                    "body": item.body,
                }
            )
        task.save(path)
        state = state.upsert(
            TaskMirrorEntry(
                provider=item.provider,
                remote_ref=item.remote_ref,
                local_task_id=task.task_id(),
                path=str(path.relative_to(room.path)),
                remote_updated_at=item.remote_updated_at,
                last_synced_at=now,
            )
        )
        written += 1
    state.save(room.task_mirrors_path)
    return written


def put_chore_item(
    room: AgentRoom, chore_id: str, request: ChorePutRequest
) -> ChoreItem:
    """Write one full local chore document and return its runtime view."""

    path = _chore_path(room, chore_id)
    ChoreFile(
        title=request.title,
        body=request.body,
        rrule=request.rrule,
        paused=request.paused,
    ).save(path)
    return _chore_item(room, path, ChoreFile.load(path))


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
            "rrule": request.rrule if request.rrule is not None else current.rrule,
            "paused": request.paused if request.paused is not None else current.paused,
        }
    )
    ChoreFile.model_validate(updated.model_dump(mode="python")).save(path)
    return _chore_item(room, path, ChoreFile.load(path))


def list_chore_items(room: AgentRoom) -> list[ChoreItem]:
    """Return local chore documents under one agent room."""

    return [
        _chore_item(room, path, document)
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
        rrule=request.rrule,
        paused=request.paused,
    ).save(path)
    return _will_item(room, path, WillFile.load(path), agent=agent)


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
            "rrule": request.rrule if request.rrule is not None else current.rrule,
            "paused": request.paused if request.paused is not None else current.paused,
        }
    )
    WillFile.model_validate(updated.model_dump(mode="python")).save(path)
    return _will_item(room, path, WillFile.load(path), agent=agent)


def load_will_item(room: AgentRoom, *, agent: AgentRef) -> WillItem | None:
    """Return the local will document for one agent room, if present."""

    path = room.will_path
    if not path.exists():
        return None
    return _will_item(room, path, WillFile.load(path), agent=agent)


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


def _load_task_mirror_state(room: AgentRoom) -> TaskMirrorState:
    path = room.task_mirrors_path
    if not path.exists():
        return TaskMirrorState()
    return TaskMirrorState.load(path)

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
    room: AgentRoom,
    path: Path,
    document: TaskFile,
    mirror_state: TaskMirrorState,
) -> TaskItem:
    task_id = document.task_id()
    mirror = mirror_state.find_by_local_task_id(task_id)
    return TaskItem(
        id=task_id,
        name=path.stem,
        body=document.body,
        status=document.status,
        requester=document.requester,
        mirrored=mirror is not None,
        provider=mirror.provider if mirror is not None else None,
        remote_ref=mirror.remote_ref if mirror is not None else None,
        thread_id=document.thread_id(),
        path=str(path),
        updated_at=_updated_at(path),
        paused=document.paused,
    )


def _chore_item(room: AgentRoom, path: Path, document: ChoreFile) -> ChoreItem:
    key = _work_key(room.chores_dir, path)
    return ChoreItem(
        id=key,
        title=document.title,
        rrule=document.rrule,
        paused=document.paused,
    )


def _will_item(
    room: AgentRoom,
    path: Path,
    document: WillFile,
    *,
    agent: AgentRef,
) -> WillItem:
    return WillItem(
        id="will",
        title=document.title,
        rrule=document.rrule,
        paused=document.paused,
    )


def _task_path(room: AgentRoom, task_name: str) -> Path:
    return _work_path(room.tasks_dir, task_name, label="Task")


def _mirror_task_path(room: AgentRoom, entry: TaskMirrorEntry) -> Path:
    stored = Path(entry.path)
    if stored.is_absolute():
        return stored
    return room.path / stored


def _new_mirror_task_path(
    room: AgentRoom, item: TaskMirrorSpec, task_id: str
) -> Path:
    slug = _task_slug(item.name) or _task_slug(item.remote_ref) or "task"
    return room.tasks_dir / item.provider / f"{slug}-{task_id[:6]}.md"


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


def _task_slug(value: str) -> str:
    text = re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")
    return text[:48]
