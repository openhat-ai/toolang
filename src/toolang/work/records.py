"""Durable work scheduler records."""

from __future__ import annotations

from dataclasses import dataclass

from toolang.catalog.types import JobKind

from .types import FileRequestStatus, JobStatus


@dataclass(frozen=True, slots=True)
class JobRecord:
    """One persisted scheduler job."""

    job_id: str
    kind: JobKind
    path: str
    definition_hash: str
    thread_id: str
    status: JobStatus
    last_run_id: str | None
    next_run_at: str | None
    run_count: int
    failed_count: int
    canceled_count: int
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class FileRequestRecord:
    """One persisted file request."""

    request_id: int
    watch_root: str
    relative_path: str
    absolute_path: str
    size: int
    mtime_ns: int
    fingerprint: str
    thread_id: str
    status: FileRequestStatus
    run_id: str
    error: str | None
    first_seen_at: str
    processed_at: str | None
    updated_at: str
