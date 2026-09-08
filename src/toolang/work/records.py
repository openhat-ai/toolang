"""Durable job scheduler records."""

from __future__ import annotations

from dataclasses import dataclass

from toolang.catalog.types import JobKind

from .types import JobStatus, JobTrigger


@dataclass(frozen=True, slots=True)
class JobRecord:
    """One persisted scheduler checkpoint."""

    job_id: str
    kind: JobKind
    revision: str
    status: JobStatus
    ready_at: str | None
    active_run_id: str | None
    active_revision: str | None
    active_trigger: JobTrigger | None
    active_at: str | None
    schedule_revision: str | None
    schedule_anchor: str | None
    next_run_at: str | None
    error: str | None
    created_at: str
    updated_at: str
