"""Caller-facing job protocol schemas."""

from __future__ import annotations

from dataclasses import dataclass

from toolang.catalog.types import JobKind, JobStage
from .types import JobStatus


@dataclass(frozen=True, slots=True)
class LastRunInfo:
    """Small run summary embedded in job runtime state."""

    id: str
    status: str
    started_at: str | None
    finished_at: str | None

@dataclass(frozen=True, slots=True)
class JobRuntimeInfo:
    """Runtime state attached to one authored job."""

    thread_id: str
    last_run: LastRunInfo | None
    next_run_at: str | None

@dataclass(frozen=True, slots=True)
class JobInfo:
    """Summary of one authored task or chore."""

    id: str
    kind: JobKind
    stage: JobStage
    status: JobStatus | None
    title: str
    path: str
    updated_at: str
    runtime: JobRuntimeInfo
    schedule: str | None = None
    remote_ref: str | None = None
    remote_status: str | None = None

@dataclass(frozen=True, slots=True)
class JobDetail(JobInfo):
    """Complete representation of one authored job."""

    body: str = ""
