"""Pure pulse-loop scanning for local tasks, chores, and will."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
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
from toolang.concepts.persisted.work import task_terminal
from toolang.runtime.requests import TurnRequestKind


@dataclass(frozen=True, slots=True)
class PulseSubmission:
    """One work item ready to enter the runtime scheduler."""

    kind: TurnRequestKind
    key: str
    thread_id: str
    text: str
    thunk: str | None = None
    model: str | None = None


def collect_pulse_submissions(
    room: AgentRoom,
    agent: AgentRef,
    state: PulseState,
    *,
    now: datetime | None = None,
    pending_keys: set[str] | None = None,
) -> tuple[PulseState, list[PulseSubmission]]:
    """Scan local work files and return updated pulse state plus due submissions."""

    current = now or datetime.now(timezone.utc)
    blocked = pending_keys or set()
    submissions: list[PulseSubmission] = []

    task_items: dict[str, PulseItemState] = {}
    for path in _markdown_paths(room.tasks_dir):
        key = _work_key(room.tasks_dir, path)
        task = TaskFile.load(path)
        item_state = state.tasks.get(key, PulseItemState())
        task_items[key] = _next_task_state(
            key=key,
            task=task,
            path=path,
            agent=agent,
            state=item_state,
            now=current,
            pending_keys=blocked,
            submissions=submissions,
        )

    chore_items: dict[str, PulseItemState] = {}
    for path in _markdown_paths(room.chores_dir):
        key = _work_key(room.chores_dir, path)
        chore = ChoreFile.load(path)
        item_state = state.chores.get(key, PulseItemState())
        chore_items[key] = _next_scheduled_state(
            kind="chore",
            key=key,
            default_thread_id=f"chore:{key}",
            title_fallback=path.stem,
            document=chore,
            state=item_state,
            now=current,
            pending_keys=blocked,
            submissions=submissions,
        )

    will_state = PulseItemState()
    if room.will_path.exists():
        will = WillFile.load(room.will_path)
        will_state = _next_scheduled_state(
            kind="will",
            key="will",
            default_thread_id=f"will:{agent.id}",
            title_fallback=agent.name,
            document=will,
            state=state.will,
            now=current,
            pending_keys=blocked,
            submissions=submissions,
        )

    return PulseState(tasks=task_items, chores=chore_items, will=will_state), submissions


def _next_task_state(
    *,
    key: str,
    task: TaskFile,
    path: Path,
    agent: AgentRef,
    state: PulseItemState,
    now: datetime,
    pending_keys: set[str],
    submissions: list[PulseSubmission],
) -> PulseItemState:
    content_hash = task.content_hash()
    next_state = state.model_copy(update={"content_hash": content_hash})
    active = (
        not task.paused
        and not task_terminal(task.status)
        and _task_matches_agent(task, agent)
    )
    pending_key = f"task:{key}"
    if pending_key in pending_keys:
        if active and state.content_hash != content_hash:
            return state
        return next_state
    if active and pending_key not in pending_keys and next_state.content_hash != state.content_hash:
        submissions.append(
            PulseSubmission(
                kind="task",
                key=key,
                thread_id=task.effective_thread_id(f"task:{key}"),
                text=task.render_input(fallback_title=path.stem),
                thunk=task.thunk,
                model=task.model,
            )
        )
        return next_state.model_copy(update={"last_enqueued_at": now})
    return next_state


def _next_scheduled_state(
    *,
    kind: TurnRequestKind,
    key: str,
    default_thread_id: str,
    title_fallback: str,
    document: ChoreFile | WillFile,
    state: PulseItemState,
    now: datetime,
    pending_keys: set[str],
    submissions: list[PulseSubmission],
) -> PulseItemState:
    pending_key = f"{kind}:{key}"
    if pending_key in pending_keys:
        return state

    content_hash = document.content_hash()
    next_due_at = state.next_due_at
    if state.content_hash != content_hash or next_due_at is None:
        next_due_at = now
    next_state = state.model_copy(
        update={
            "content_hash": content_hash,
            "next_due_at": None if document.paused else next_due_at,
        }
    )
    if document.paused:
        return next_state

    due_at = next_state.next_due_at
    if due_at is None or due_at > now:
        return next_state

    text = document.render_input(fallback_title=title_fallback)
    if not text.strip():
        return next_state.model_copy(
            update={"last_enqueued_at": None, "next_due_at": now + timedelta(seconds=document.interval_sec)}
        )

    submissions.append(
        PulseSubmission(
            kind=kind,
            key=key,
            thread_id=document.effective_thread_id(default_thread_id),
            text=text,
            thunk=document.thunk,
            model=document.model,
        )
    )
    return next_state.model_copy(
        update={
            "last_enqueued_at": now,
            "next_due_at": now + timedelta(seconds=document.interval_sec),
        }
    )


def _task_matches_agent(task: TaskFile, agent: AgentRef) -> bool:
    assignee = (task.assignee or "").strip()
    if not assignee:
        return True
    if assignee == "self":
        return True
    return assignee in {agent.uri, agent.id, agent.id[:12], agent.name}


def _markdown_paths(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return sorted(path for path in root.rglob("*.md") if path.is_file())


def _work_key(root: Path, path: Path) -> str:
    relative = path.relative_to(root)
    parts = list(relative.parts)
    if parts:
        parts[-1] = Path(parts[-1]).stem
    return "/".join(parts)
