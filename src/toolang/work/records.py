"""Durable chore scheduler records."""

from __future__ import annotations

from dataclasses import dataclass

from .types import ChoreStatus, ChoreTrigger


@dataclass(frozen=True, slots=True)
class ChoreRecord:
    """One persisted scheduler checkpoint."""

    chore_id: str
    name: str
    revision: str
    status: ChoreStatus
    ready_at: str | None
    active_run_id: str | None
    active_revision: str | None
    active_trigger: ChoreTrigger | None
    active_at: str | None
    schedule_revision: str | None
    schedule_anchor: str | None
    next_run_at: str | None
    error: str | None
    created_at: str
    updated_at: str
