"""Immutable effective job definitions."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
import re

from toolang.catalog.job import AuthoredJobs, JobFile
from toolang.catalog.types import DEFAULT_CHORE_SCHEDULE, JobKind
from toolang.common.layout import AgentLayout
from toolang.lang.ast import JobDecl, Program

from .authoring import assign_missing_authored_job_ids

_REMOTE_REF_PATTERN = re.compile(r"\b[A-Z][A-Z0-9]+-\d+\b")


@dataclass(frozen=True, slots=True)
class Job:
    """One current executable task or chore definition."""

    id: str
    kind: JobKind
    title: str | None
    body: str
    schedule: str | None
    revision: str
    source: str
    path: Path | None

    @property
    def thread_id(self) -> str:
        """Return the stable execution thread derived from job identity."""

        return f"{self.kind}_{self.id}"


def load_ready_jobs(layout: AgentLayout) -> tuple[Job, ...]:
    """Load normalized ready Markdown jobs for one agent home."""

    catalog = AuthoredJobs(layout.home)
    ready = catalog.list(stage="ready")
    if any(item.optional_id is None for item in ready):
        assign_missing_authored_job_ids(layout, catalog=catalog, stage="ready")
        ready = catalog.list(stage="ready")
    return tuple(_file_job(layout.root, item) for item in ready)


def program_jobs(program: Program) -> tuple[Job, ...]:
    """Normalize program task and chore declarations."""

    return tuple(_program_job(decl) for decl in program.jobs)


def merge_jobs(*groups: Iterable[Job]) -> dict[str, Job]:
    """Merge effective job sources while rejecting every duplicate id."""

    merged: dict[str, Job] = {}
    for group in groups:
        for job in group:
            previous = merged.get(job.id)
            if previous is not None:
                raise ValueError(
                    f"duplicate job id {job.id!r}: {previous.source} and {job.source}"
                )
            merged[job.id] = job
    return merged


def schedule_revision(job: Job) -> str | None:
    """Return the stable revision of one chore schedule."""

    if job.schedule is None:
        return None
    return sha256(job.schedule.encode()).hexdigest()


def _file_job(root: Path, job: JobFile) -> Job:
    if job.path is None:
        raise ValueError("authored job path is required")
    return _job(
        job_id=job.id,
        kind=job.kind,
        title=job.title,
        body=job.body,
        schedule=job.schedule if job.kind == "chore" else None,
        source=str(job.path.relative_to(root)),
        path=job.path,
    )


def _program_job(decl: JobDecl) -> Job:
    title = str(decl.meta.get("title") or "").strip()
    return _job(
        job_id=decl.name,
        kind=decl.kind,
        title=title or None,
        body=decl.body.strip(),
        schedule=(
            str(decl.meta.get("schedule") or DEFAULT_CHORE_SCHEDULE).strip()
            if decl.kind == "chore"
            else None
        ),
        source=f"agent.too:{decl.span.line}",
        path=None,
    )


def _job(
    *,
    job_id: str,
    kind: JobKind,
    title: str | None,
    body: str,
    schedule: str | None,
    source: str,
    path: Path | None,
) -> Job:
    normalized_body = body.replace("\r\n", "\n").strip()
    return Job(
        id=job_id,
        kind=kind,
        title=title,
        body=body,
        schedule=schedule,
        revision=sha256(normalized_body.encode()).hexdigest(),
        source=source,
        path=path,
    )


def job_thread_id(job: JobFile) -> str:
    """Return the runtime thread projection for one authored job."""

    return f"{job.kind}_{job.id}"


def job_display_title(job: JobFile, *, fallback: str) -> str:
    """Return a short title for a caller-facing job projection."""

    if job.title:
        return job.title
    for line in job.body.splitlines():
        candidate = line.strip().lstrip("#").strip()
        if candidate:
            return candidate[:80]
    return fallback


def job_remote_status(job: JobFile) -> str | None:
    """Return the first explicit remote status in a task body."""

    if job.kind != "task":
        return None
    for line in job.body.splitlines():
        key, separator, value = line.partition(":")
        if separator == ":" and key.strip().lower() in {"status", "remote status"}:
            return value.strip() or None
    return None


def job_remote_ref(job: JobFile) -> str | None:
    """Return a remote work-item key projected from authored task content."""

    if job.kind != "task":
        return None
    text = "\n".join(part for part in (job.title or "", job.body) if part)
    match = _REMOTE_REF_PATTERN.search(text)
    return None if match is None else match.group(0)
