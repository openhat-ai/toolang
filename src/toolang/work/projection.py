"""Project authored and scheduled jobs into caller-facing schemas."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from toolang.catalog.job import AuthoredJobs, JobFile
from toolang.catalog.types import JobKind, JobStage
from toolang.lang.ast import Program
from .authoring import assign_missing_authored_job_ids
from .schemas import JobDetail, JobInfo, JobRuntimeInfo, LastRunInfo
from .state import (
    AgentJobs,
    job_display_title,
    job_remote_ref,
    job_remote_status,
    job_thread_id,
)
from .store import JobRecord, open_job_store


class JobRun(Protocol):
    """Execution fields required by job inspection."""

    @property
    def run_id(self) -> str: ...

    @property
    def thread_id(self) -> str: ...

    @property
    def status(self) -> str: ...

    @property
    def created_at(self) -> str: ...

    @property
    def started_at(self) -> str | None: ...

    @property
    def finished_at(self) -> str | None: ...

class JobProjector:
    """Project authored jobs with scheduler and execution state in one batch."""

    def __init__(
        self,
        *,
        catalog: AuthoredJobs,
        home: Path,
        records: dict[tuple[JobKind, str], JobRecord],
        latest_runs: dict[str, JobRun],
    ) -> None:
        self.catalog = catalog
        self.home = home
        self.records = records
        self.latest_runs = latest_runs

    @classmethod
    def load(
        cls,
        *,
        root: Path,
        agent_name: str,
        home: Path,
        program: Program,
        runs: Iterable[JobRun],
    ) -> JobProjector:
        """Load one consistent inspection snapshot from the owning stores."""

        catalog = AuthoredJobs(home)
        assign_missing_authored_job_ids(root, agent_name, catalog=catalog)
        job_store = open_job_store(root, agent_name)
        try:
            records = job_store.reconcile(
                jobs=AgentJobs.load(root, agent_name, program)
            )
        finally:
            job_store.close()
        latest_runs: dict[str, JobRun] = {}
        for run in runs:
            current = latest_runs.get(run.thread_id)
            if current is None or run.created_at > current.created_at:
                latest_runs[run.thread_id] = run
        return cls(
            catalog=catalog,
            home=home,
            records={(record.kind, record.job_id): record for record in records},
            latest_runs=latest_runs,
        )

    def list(
        self,
        *,
        kind: JobKind | None = None,
        stage: JobStage = "ready",
    ) -> tuple[JobInfo, ...]:
        return tuple(
            self.info(job) for job in self.catalog.list(kind=kind, stage=stage)
        )

    def detail(self, job: JobFile) -> JobDetail:
        info = self.info(job)
        return JobDetail(
            id=info.id,
            kind=info.kind,
            stage=info.stage,
            status=info.status,
            title=info.title,
            path=info.path,
            updated_at=info.updated_at,
            runtime=info.runtime,
            schedule=info.schedule,
            remote_ref=info.remote_ref,
            remote_status=info.remote_status,
            body=job.body,
        )

    def info(self, job: JobFile) -> JobInfo:
        if job.path is None:
            raise ValueError("authored job path is required")
        record = self.records.get((job.kind, job.id)) if job.stage == "ready" else None
        thread_id = job_thread_id(job)
        last_run = self.latest_runs.get(thread_id)
        runtime = JobRuntimeInfo(
            thread_id=thread_id,
            last_run=(
                LastRunInfo(
                    id=last_run.run_id,
                    status=last_run.status,
                    started_at=last_run.started_at,
                    finished_at=last_run.finished_at,
                )
                if last_run is not None
                else None
            ),
            next_run_at=record.next_run_at if record is not None else None,
        )
        return JobInfo(
            id=job.id,
            kind=job.kind,
            stage=job.stage,
            status=record.status if record is not None else None,
            schedule=job.schedule if job.kind == "chore" else None,
            remote_ref=job_remote_ref(job),
            remote_status=job_remote_status(job),
            title=job_display_title(job, fallback=job.path.stem),
            path=self._relative_path(job.path),
            updated_at=datetime.fromtimestamp(
                job.path.stat().st_mtime_ns / 1_000_000_000,
                tz=timezone.utc,
            ).isoformat(),
            runtime=runtime,
        )

    def _relative_path(self, path: Path) -> str:
        try:
            return str(path.relative_to(self.home))
        except ValueError:
            return str(path)
