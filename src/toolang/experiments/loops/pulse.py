"""Pulse loop for local tasks and chores."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Literal

from .. import agents, work
from ..execution.runner import RunRequest
from ..state.pulse import PulseItemState, PulseState

if TYPE_CHECKING:
    from ..up import UptimeContext
    from ..execution.runner import RunOutcome

DEFAULT_INTERVAL_MS = 30_000.0


@dataclass(frozen=True, slots=True)
class PulseSubmission:
    """One work item ready to enter the runtime queue."""

    kind: Literal["task", "chore"]
    key: str
    thread_id: str
    text: str


def spawn(
    context: UptimeContext,
    *,
    stop_signal: asyncio.Event,
) -> asyncio.Task[None]:
    """Spawn the pulse loop in one background task."""

    return asyncio.create_task(run(context, stop_signal=stop_signal))


async def run(
    context: UptimeContext,
    *,
    stop_signal: asyncio.Event,
) -> None:
    """Scan live work items and enqueue due runs until the runtime stops."""
    interval_value = context.config.require("loops.pulse.interval_ms")
    if not isinstance(interval_value, int | float):
        raise TypeError("invalid config: loops.pulse.interval_ms")
    interval_timeout = float(interval_value) / 1000
    state = _load_pulse_state(context)
    seen_completed: set[str] = set()

    while True:
        now = datetime.now(timezone.utc)
        _record_completed_runs(state, context.runner.completed(), seen_completed=seen_completed, now=now)
        state, submissions = collect_pulse_submissions(
            context,
            state,
            now=now,
            pending_keys=_pending_keys(context),
        )
        for submission in submissions:
            context.runner.enqueue(
                RunRequest(
                    group="pulse",
                    origin=submission.kind,
                    thread_id=submission.thread_id,
                    thunk=submission.text,
                    metadata={"pulse_key": submission.key},
                )
            )
        _save_pulse_state(context, state)
        try:
            await asyncio.wait_for(stop_signal.wait(), timeout=interval_timeout)
        except TimeoutError:
            continue
        else:
            return


def collect_pulse_submissions(
    context: UptimeContext,
    state: PulseState,
    *,
    now: datetime | None = None,
    pending_keys: set[str] | None = None,
) -> tuple[PulseState, list[PulseSubmission]]:
    """Scan live task and chore entries and return due submissions."""

    current = now or datetime.now(timezone.utc)
    blocked = pending_keys or set()
    submissions: list[PulseSubmission] = []

    task_items: dict[str, PulseItemState] = {}
    for entry in context.live.job_entries:
        if entry.kind != "task":
            continue
        path = context.root / entry.path
        document = work.TaskFile.load(path, persist_id=True)
        task_id = document.task_id()
        item_state = state.tasks.get(task_id, PulseItemState())
        task_items[task_id] = _next_task_state(
            task=document,
            path_name=entry.name,
            state=item_state,
            now=current,
            pending_keys=blocked,
            submissions=submissions,
        )

    chore_items: dict[str, PulseItemState] = {}
    for entry in context.live.job_entries:
        if entry.kind != "chore":
            continue
        path = context.root / entry.path
        document = work.ChoreFile.load(path)
        item_state = state.chores.get(entry.name, PulseItemState())
        chore_items[entry.name] = _next_chore_state(
            chore=document,
            key=entry.name,
            state=item_state,
            now=current,
            pending_keys=blocked,
            submissions=submissions,
        )

    return PulseState(tasks=task_items, chores=chore_items), submissions


def _next_task_state(
    *,
    task: work.TaskFile,
    path_name: str,
    state: PulseItemState,
    now: datetime,
    pending_keys: set[str],
    submissions: list[PulseSubmission],
) -> PulseItemState:
    task_id = task.task_id()
    content_hash = task.content_hash()
    next_state = state.model_copy(update={"content_hash": content_hash})
    active = not task.paused and not work.task_terminal(task.status)
    pending_key = f"task:{task_id}"
    if pending_key in pending_keys:
        if active and state.content_hash != content_hash:
            return state
        return next_state
    if active and next_state.content_hash != state.content_hash:
        submissions.append(
            PulseSubmission(
                kind="task",
                key=task_id,
                thread_id=task.thread_id(),
                text=task.render_input(fallback_name=path_name),
            )
        )
        return next_state.model_copy(update={"last_enqueued_at": now})
    return next_state


def _next_chore_state(
    *,
    chore: work.ChoreFile,
    key: str,
    state: PulseItemState,
    now: datetime,
    pending_keys: set[str],
    submissions: list[PulseSubmission],
) -> PulseItemState:
    pending_key = f"chore:{key}"
    if pending_key in pending_keys:
        return state

    content_hash = chore.content_hash()
    next_due_at = state.next_due_at
    if state.content_hash != content_hash or next_due_at is None:
        next_due_at = now
    next_state = state.model_copy(
        update={
            "content_hash": content_hash,
            "next_due_at": None if chore.paused else next_due_at,
        }
    )
    if chore.paused:
        return next_state

    due_at = next_state.next_due_at
    if due_at is None or due_at > now:
        return next_state

    text = chore.render_input(fallback_title=key.rsplit("/", 1)[-1])
    if not text.strip():
        return next_state.model_copy(
            update={
                "last_enqueued_at": None,
                "next_due_at": work.next_scheduled_at(
                    chore.rrule,
                    anchor=due_at,
                    not_before=now,
                    inclusive=False,
                ),
            }
        )

    submissions.append(
        PulseSubmission(
            kind="chore",
            key=key,
            thread_id=f"chore:{key}",
            text=text,
        )
    )
    return next_state.model_copy(
        update={
            "last_enqueued_at": now,
            "next_due_at": work.next_scheduled_at(
                chore.rrule,
                anchor=due_at,
                not_before=now,
                inclusive=False,
            ),
        }
    )


def _load_pulse_state(context: UptimeContext) -> PulseState:
    path = agents.agent_pulse_state_path(context.root, context.name)
    if not path.is_file():
        return PulseState()
    try:
        return PulseState.load(path)
    except Exception:
        return PulseState()


def _save_pulse_state(context: UptimeContext, state: PulseState) -> None:
    state.save(agents.agent_pulse_state_path(context.root, context.name))


def _pending_keys(context: UptimeContext) -> set[str]:
    keys: set[str] = set()
    for request in (*context.runner.pending_requests(), *context.runner.active_requests()):
        key = _request_key(request)
        if key is not None:
            keys.add(key)
    return keys


def _request_key(request: RunRequest) -> str | None:
    if request.group != "pulse" or request.thread_id is None:
        return None
    if request.origin == "task":
        task_id = work.task_id_from_thread_id(request.thread_id)
        return f"task:{task_id}" if task_id is not None else None
    if request.origin == "chore":
        key = request.thread_id.removeprefix("chore:").strip()
        return f"chore:{key}" if key else None
    return None


def _record_completed_runs(
    state: PulseState,
    results: list["RunOutcome"],
    *,
    seen_completed: set[str],
    now: datetime,
) -> None:
    for result in results:
        if result.run_id in seen_completed:
            continue
        seen_completed.add(result.run_id)
        if result.origin == "task" and result.thread_id is not None:
            task_id = work.task_id_from_thread_id(result.thread_id)
            if task_id is None:
                continue
            item_state = state.tasks.get(task_id)
            if item_state is None:
                continue
            item_state.last_started_at = now
            item_state.last_finished_at = now
            item_state.last_status = result.status
            item_state.last_run_id = result.run_id
            continue
        if result.origin == "chore" and result.thread_id is not None:
            key = result.thread_id.removeprefix("chore:").strip()
            item_state = state.chores.get(key)
            if item_state is None:
                continue
            item_state.last_started_at = now
            item_state.last_finished_at = now
            item_state.last_status = result.status
            item_state.last_run_id = result.run_id
