"""Caller-facing chore protocol schemas."""

from __future__ import annotations

from dataclasses import dataclass

from .types import ChoreStatus


@dataclass(frozen=True, slots=True)
class LastRunInfo:
    id: str
    status: str
    started_at: str | None
    finished_at: str | None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class ChoreRuntimeInfo:
    """Checkpoint state; synced indicates whether it describes the current definition."""

    thread_id: str
    synced: bool
    status: ChoreStatus | None
    active_run_id: str | None
    pending_at: str | None
    next_scheduled_at: str | None
    last_run: LastRunInfo | None
    scheduler_error: str | None
    updated_at: str | None


@dataclass(frozen=True, slots=True)
class ChoreInfo:
    id: str
    name: str
    ref: str
    title: str
    path: str
    line: int | None
    schedule: str | None
    source_error: str | None
    runtime: ChoreRuntimeInfo


@dataclass(frozen=True, slots=True)
class ChoreDetail(ChoreInfo):
    body: str
