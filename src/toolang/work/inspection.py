"""Aggregate authored, scheduler, and execution state for job inspection."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Protocol

from toolang.catalog.job import AuthoredJobs, JobFile
from toolang.catalog.types import JobKind, JobStage
from toolang.common.layout import AgentLayout
from toolang.lang.ast import Program

from .authoring import assign_missing_authored_job_ids
from .records import JobRecord
from .schemas import JobDetail, JobInfo, LastRunInfo
from .state import AgentJobs, job_thread_id
from .store import open_job_store


class JobRun(Protocol):
    """Execution fields required by job inspection."""

    id: str
    thread: str
    status: str
    created_at: str
    started_at: str | None
    finished_at: str | None


class JobInspection:
    """Inspect authored jobs with scheduler and execution state in one batch."""

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
        layout: AgentLayout,
        program: Program,
        runs: Iterable[JobRun],
    ) -> JobInspection:
        """Load one consistent inspection snapshot from the owning stores."""

        catalog = AuthoredJobs(layout.home)
        assign_missing_authored_job_ids(
            layout,
            catalog=catalog,
        )
        job_store = open_job_store(layout)
        try:
            records = job_store.reconcile(jobs=AgentJobs.load(layout, program))
        finally:
            job_store.close()
        latest_runs: dict[str, JobRun] = {}
        for run in runs:
            current = latest_runs.get(run.thread)
            if current is None or run.created_at > current.created_at:
                latest_runs[run.thread] = run
        return cls(
            catalog=catalog,
            home=layout.home,
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
            self._info(job) for job in self.catalog.list(kind=kind, stage=stage)
        )

    def detail(self, job: JobFile) -> JobDetail:
        record, last_run = self._state(job)
        return JobDetail.from_job(
            job,
            home=self.home,
            record=record,
            last_run=last_run,
        )

    def _info(self, job: JobFile) -> JobInfo:
        record, last_run = self._state(job)
        return JobInfo.from_job(
            job,
            home=self.home,
            record=record,
            last_run=last_run,
        )

    def _state(self, job: JobFile) -> tuple[JobRecord | None, LastRunInfo | None]:
        record = self.records.get((job.kind, job.id)) if job.stage == "ready" else None
        run = self.latest_runs.get(job_thread_id(job))
        last_run = (
            LastRunInfo(
                id=run.id,
                status=run.status,
                started_at=run.started_at,
                finished_at=run.finished_at,
            )
            if run is not None
            else None
        )
        return record, last_run
