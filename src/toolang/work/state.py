"""Immutable authored and effective job job_definitions."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path

from ..lang.ast import JobDecl, Program
from . import definitions as job_definitions


@dataclass(frozen=True, slots=True)
class JobDefinition:
    """One executable task or chore definition."""

    id: str
    kind: job_definitions.JobKind
    name: str
    title: str | None
    body: str
    source: str
    path: str | None
    input: str
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
    def load(cls, root: Path, name: str) -> HomeJobs:
        jobs = [
            *(
                _file_definition(root, entry)
                for entry in job_definitions._list_tasks(root, name)
            ),
            *(
                _file_definition(root, entry)
                for entry in job_definitions._list_chores(root, name)
            ),
        ]
        return cls(tuple(sorted(jobs, key=lambda item: (item.kind, item.id))))


@dataclass(frozen=True, slots=True)
class AgentJobs:
    """Effective jobs from one home snapshot and one immutable program."""

    definitions: tuple[JobDefinition, ...] = ()

    @classmethod
    def load(cls, root: Path, name: str, program: Program) -> AgentJobs:
        return cls.merge(HomeJobs.load(root, name), program)

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

    def get(self, kind: job_definitions.JobKind, job_id: str) -> JobDefinition | None:
        return next(
            (job for job in self.definitions if job.kind == kind and job.id == job_id),
            None,
        )


def _file_definition(
    root: Path,
    entry: job_definitions.TaskEntry | job_definitions.ChoreEntry,
) -> JobDefinition:
    if isinstance(entry, job_definitions.TaskEntry):
        job_id = entry.document.task_id()
        kind: job_definitions.JobKind = "task"
        title = entry.document.title
        body = entry.document.body
        input_text = entry.document.render_input(
            fallback_name=entry.name.rsplit("/", 1)[-1]
        )
        schedule = None
    else:
        job_id = entry.document.chore_id()
        kind = "chore"
        title = entry.document.title
        body = entry.document.body
        input_text = entry.document.render_input(
            fallback_title=entry.name.rsplit("/", 1)[-1]
        )
        schedule = entry.document.schedule
    source = str(entry.path.relative_to(root))
    return _definition(
        job_id=job_id,
        kind=kind,
        name=entry.name.rsplit("/", 1)[-1],
        title=title,
        body=body,
        source=source,
        path=str(entry.path),
        input_text=input_text,
        schedule=schedule,
    )


def _program_definition(decl: JobDecl) -> JobDefinition:
    title = str(decl.meta.get("title") or "").strip()
    body = decl.body.strip()
    input_text = (
        f"# {title}\n\n{body}" if title and body else body or title or decl.name
    )
    schedule = (
        str(decl.meta.get("schedule") or job_definitions.DEFAULT_CHORE_SCHEDULE).strip()
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
        input_text=input_text,
        schedule=schedule,
    )


def _definition(
    *,
    job_id: str,
    kind: job_definitions.JobKind,
    name: str,
    title: str | None,
    body: str,
    source: str,
    path: str | None,
    input_text: str,
    schedule: str | None,
) -> JobDefinition:
    payload = json.dumps(
        [job_id, kind, source, input_text, schedule],
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
        input=input_text,
        schedule=schedule,
        fingerprint=sha256(payload.encode()).hexdigest(),
        thread=job_definitions.job_thread_id(kind, job_id),
    )


def _key(job: JobDefinition) -> tuple[job_definitions.JobKind, str]:
    return job.kind, job.id


def _index_jobs(
    jobs: Iterable[JobDefinition], *, source: str
) -> dict[tuple[job_definitions.JobKind, str], JobDefinition]:
    indexed: dict[tuple[job_definitions.JobKind, str], JobDefinition] = {}
    for job in jobs:
        key = _key(job)
        if key in indexed:
            raise ValueError(f"duplicate {source} {job.kind} id: {job.id}")
        indexed[key] = job
    return indexed
