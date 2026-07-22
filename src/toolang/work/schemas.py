"""Caller-facing job protocol schemas."""

from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import datetime, timezone
from pathlib import Path

from toolang.catalog.job import JobFile
from toolang.catalog.types import JobKind, JobStage
from .records import JobRecord
from .state import job_display_title, job_remote_ref, job_remote_status, job_thread_id
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

    @classmethod
    def from_job(
        cls,
        job: JobFile,
        *,
        home: Path,
        record: JobRecord | None,
        last_run: LastRunInfo | None,
    ) -> JobInfo:
        """Build caller-facing job information from authored and durable state."""

        if job.path is None:
            raise ValueError("authored job path is required")
        try:
            path = str(job.path.relative_to(home))
        except ValueError:
            path = str(job.path)
        return cls(
            id=job.id,
            kind=job.kind,
            stage=job.stage,
            status=record.status if record is not None else None,
            schedule=job.schedule if job.kind == "chore" else None,
            remote_ref=job_remote_ref(job),
            remote_status=job_remote_status(job),
            title=job_display_title(job, fallback=job.path.stem),
            path=path,
            updated_at=datetime.fromtimestamp(
                job.path.stat().st_mtime_ns / 1_000_000_000,
                tz=timezone.utc,
            ).isoformat(),
            runtime=JobRuntimeInfo(
                thread_id=job_thread_id(job),
                last_run=last_run,
                next_run_at=record.next_run_at if record is not None else None,
            ),
        )


@dataclass(frozen=True, slots=True)
class JobDetail(JobInfo):
    """Complete representation of one authored job."""

    body: str = ""

    @classmethod
    def from_job(
        cls,
        job: JobFile,
        *,
        home: Path,
        record: JobRecord | None,
        last_run: LastRunInfo | None,
    ) -> JobDetail:
        """Build complete caller-facing job detail."""

        info = JobInfo.from_job(
            job,
            home=home,
            record=record,
            last_run=last_run,
        )
        return cls(
            **{item.name: getattr(info, item.name) for item in fields(JobInfo)},
            body=job.body,
        )
