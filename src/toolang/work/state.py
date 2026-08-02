"""Immutable authored and effective job definitions."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
import re

from ..lang.ast import JobDecl, Program
from toolang.catalog.job import AuthoredJobs, JobFile
from toolang.catalog.types import DEFAULT_CHORE_SCHEDULE, JobKind
from toolang.common.layout import AgentLayout
from .authoring import assign_missing_authored_job_ids

_REMOTE_REF_PATTERN = re.compile(r"\b[A-Z][A-Z0-9]+-\d+\b")


@dataclass(frozen=True, slots=True)
class JobDefinition:
    """One executable task or chore definition."""

    id: str
    kind: JobKind
    name: str
    title: str | None
    body: str
    source: str
    path: str | None
    schedule: str | None
    fingerprint: str
    thread: str

    def run_metadata(self) -> dict[str, object]:
        """Return the job context captured by a run request."""

        return {
            "kind": self.kind,
            "id": self.id,
            "provider": "local",
            "name": self.name,
            "title": self.title,
            "body": self.body,
            "schedule": self.schedule,
            "thread_id": self.thread,
            "source": self.source,
            "path": self.path,
            "readable": self.path is not None,
            "writable": self.kind == "task" and self.path is not None,
            "commentable": False,
        }


@dataclass(frozen=True, slots=True)
class HomeJobs:
    """Immutable snapshot of ready task and chore files."""

    definitions: tuple[JobDefinition, ...] = ()

    @classmethod
    def load(cls, layout: AgentLayout) -> HomeJobs:
        catalog = AuthoredJobs(layout.home)
        assign_missing_authored_job_ids(
            layout,
            catalog=catalog,
        )
        jobs = [_file_definition(layout.root, job) for job in catalog.list()]
        return cls(tuple(sorted(jobs, key=lambda item: (item.kind, item.id))))


@dataclass(frozen=True, slots=True)
class AgentJobs:
    """Effective jobs from one home snapshot and one immutable program."""

    definitions: tuple[JobDefinition, ...] = ()

    @classmethod
    def load(cls, layout: AgentLayout, program: Program) -> AgentJobs:
        return cls.merge(HomeJobs.load(layout), program)

    @classmethod
    def merge(cls, home: HomeJobs, program: Program) -> AgentJobs:
        program_jobs = _index_jobs(
            map(_program_definition, program.jobs), source="program"
        )
        home_jobs = _index_jobs(home.definitions, source="home")
        program_jobs.update(home_jobs)
        return cls(
            tuple(sorted(program_jobs.values(), key=lambda item: (item.kind, item.id)))
        )

    def get(self, kind: JobKind, job_id: str) -> JobDefinition | None:
        return next(
            (job for job in self.definitions if job.kind == kind and job.id == job_id),
            None,
        )


def _file_definition(
    root: Path,
    job: JobFile,
) -> JobDefinition:
    if job.path is None:
        raise ValueError("authored job path is required")
    name = job.name
    source = str(job.path.relative_to(root))
    return _definition(
        job_id=job.id,
        kind=job.kind,
        name=name,
        title=job.title,
        body=job.body,
        source=source,
        path=str(job.path),
        schedule=job.schedule if job.kind == "chore" else None,
    )


def _program_definition(decl: JobDecl) -> JobDefinition:
    title = str(decl.meta.get("title") or "").strip()
    body = decl.body.strip()
    schedule = (
        str(decl.meta.get("schedule") or DEFAULT_CHORE_SCHEDULE).strip()
        if decl.kind == "chore"
        else None
    )
    return _definition(
        job_id=decl.name,
        kind=decl.kind,
        name=decl.name,
        title=title or None,
        body=body,
        source=f"agent.too:{decl.span.line}",
        path=None,
        schedule=schedule,
    )


def _definition(
    *,
    job_id: str,
    kind: JobKind,
    name: str,
    title: str | None,
    body: str,
    source: str,
    path: str | None,
    schedule: str | None,
) -> JobDefinition:
    payload = json.dumps(
        [job_id, kind, source, title, body, schedule],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return JobDefinition(
        id=job_id,
        kind=kind,
        name=name,
        title=title,
        body=body,
        source=source,
        path=path,
        schedule=schedule,
        fingerprint=sha256(payload.encode()).hexdigest(),
        thread=f"{kind}_{job_id.strip()}",
    )


def _key(job: JobDefinition) -> str:
    return job.id


def _index_jobs(
    jobs: Iterable[JobDefinition], *, source: str
) -> dict[str, JobDefinition]:
    indexed: dict[str, JobDefinition] = {}
    for job in jobs:
        key = _key(job)
        if key in indexed:
            raise ValueError(f"duplicate {source} job id: {job.id}")
        indexed[key] = job
    return indexed


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
