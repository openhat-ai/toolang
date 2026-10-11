"""Durable chore scheduler records."""

from __future__ import annotations

from dataclasses import dataclass

from .types import ChoreStatus, ChoreTrigger


@dataclass(frozen=True, slots=True)
class ChoreRecord:
    """One persisted scheduler checkpoint."""

    chore_id: str
    name: str
    body_hash: str
    status: ChoreStatus
    pending_at: str | None
    pending_trigger: ChoreTrigger | None
    active_run_id: str | None
    active_trigger: ChoreTrigger | None
    active_due_at: str | None
    schedule_hash: str | None
    schedule_anchor_at: str | None
    next_scheduled_at: str | None
    error: str | None
    created_at: str
    updated_at: str
