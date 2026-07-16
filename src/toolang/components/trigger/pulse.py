"""Pulse loop for ready task and chore scheduling."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Literal, cast

from ... import jobs, work
from ...execution.input import allocate_run_id
from ...execution.records import RunStatus
from ...execution.runner import RunRequest

if TYPE_CHECKING:
    from ...up import UptimeContext
    from ...execution.runner import RunOutcome

DEFAULT_INTERVAL_MS = 30_000.0


@dataclass(frozen=True, slots=True)
class PulseSubmission:
    """One claimed job ready to enter the runtime queue."""

    kind: work.JobKind
    key: str
    run_id: str
    thread_id: str
    text: str
    trigger: jobs.JobTrigger


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
    """Scan ready work items and enqueue due runs until the runtime stops."""

    interval_value = context.config.require("components.trigger.pulse.interval_ms")
    if not isinstance(interval_value, int | float):
        raise TypeError("invalid config: components.trigger.pulse.interval_ms")
    interval_timeout = float(interval_value) / 1000
    seen_completed: set[str] = set()
    job_store = jobs.open_job_store(context.root, context.name)
    try:
        while True:
            now = datetime.now(timezone.utc)
            _record_completed_runs(
                context,
                job_store,
                context.runner.completed(),
                seen_completed=seen_completed,
                now=now,
            )
            submissions = collect_pulse_submissions(
                context,
                job_store,
                now=now,
            )
            for submission in submissions:
                context.runner.enqueue(
                    RunRequest(
                        group=f"pulse:{submission.kind}",
                        origin=submission.kind,
                        run_id=submission.run_id,
                        thread_id=submission.thread_id,
                        thunk=submission.text,
                        metadata={
                            "pulse_key": submission.key,
                            "job_trigger": submission.trigger,
                        },
                    )
                )
            try:
                await asyncio.wait_for(stop_signal.wait(), timeout=interval_timeout)
            except TimeoutError:
                continue
            else:
                return
    finally:
        job_store.close()


def collect_pulse_submissions(
    context: UptimeContext,
    job_store: jobs.JobStore,
    *,
    now: datetime | None = None,
) -> list[PulseSubmission]:
    """Reconcile ready task/chore files and return due claimed submissions."""

    current = now or datetime.now(timezone.utc)
    submissions: list[PulseSubmission] = []
    for kind in ("task", "chore"):
        if not _run_component_enabled(context, kind):
            continue
        job_store.reconcile(
            toolang_root=context.root,
            agent_name=context.name,
            kind=kind,
            now=current,
        )
        while True:
            claimed = job_store.claim_due(
                toolang_root=context.root,
                agent_name=context.name,
                kind=kind,
                run_id=allocate_run_id(context),
                now=current,
            )
            if claimed is None:
                break
            if not claimed.text.strip():
                job_store.finish_run(
                    run_id=claimed.run_id,
                    run_status="finished",
                    now=current,
                    toolang_root=context.root,
                    agent_name=context.name,
                )
                continue
            submissions.append(
                PulseSubmission(
                    kind=kind,
                    key=claimed.job.job_id,
                    run_id=claimed.run_id,
                    thread_id=claimed.job.thread_id,
                    text=claimed.text,
                    trigger=claimed.trigger,
                )
            )
    return submissions


def _run_component_enabled(context: UptimeContext, kind: Literal["task", "chore"]) -> bool:
    enabled_components = context.config.require("components.enabled")
    legacy_enabled = context.config.get("features.enabled")
    return (
        isinstance(enabled_components, tuple)
        and f"runner.{kind}" in enabled_components
        or isinstance(legacy_enabled, tuple)
        and "pulse" in legacy_enabled
    )


def _record_completed_runs(
    context: "UptimeContext",
    job_store: jobs.JobStore,
    results: list["RunOutcome"],
    *,
    seen_completed: set[str],
    now: datetime,
) -> None:
    for result in results:
        if result.run_id in seen_completed:
            continue
        seen_completed.add(result.run_id)
        if result.origin not in {"task", "chore"}:
            continue
        stored = context.store.get_run(run_id=result.run_id)
        status: RunStatus = stored.status if stored is not None else _outcome_status(result.status)
        job_store.finish_run(
            run_id=result.run_id,
            run_status=status,
            now=now,
            toolang_root=context.root,
            agent_name=context.name,
        )


def _outcome_status(status: str) -> RunStatus:
    if status in {"finished", "failed", "canceled"}:
        return cast(RunStatus, status)
    return "failed"
