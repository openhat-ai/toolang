"""Self-driven task and chore scheduling."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timezone

from ..execution.executor import Executor
from ..execution.records import RunRecord
from ..execution.request import RunRequest
from ..state.state import AgentState
from toolang.catalog.job import JobKind
from .state import AgentJobs, HomeJobs
from .store import ClaimedJob, JobStore

DEFAULT_INTERVAL_MS = 30_000.0


class Scheduler:
    """Claim due jobs and execute them against captured agent state."""

    def __init__(
        self,
        *,
        job_store: JobStore,
        executor: Executor,
        get_home_jobs: Callable[[], HomeJobs],
        get_agent_state: Callable[[], AgentState],
        kinds: tuple[JobKind, ...] = ("task", "chore"),
        interval_ms: float = DEFAULT_INTERVAL_MS,
    ) -> None:
        self.job_store = job_store
        self.executor = executor
        self.get_home_jobs = get_home_jobs
        self.get_agent_state = get_agent_state
        self.kinds = kinds
        self.interval = interval_ms / 1000
        self._active: dict[str, tuple[asyncio.Task[RunRecord], AgentJobs]] = {}

    def start(self, *, stop_signal: asyncio.Event) -> asyncio.Task[None]:
        return asyncio.create_task(self.run(stop_signal=stop_signal))

    async def run(self, *, stop_signal: asyncio.Event) -> None:
        try:
            while not stop_signal.is_set():
                self.run_once()
                self._finish_completed()
                try:
                    await asyncio.wait_for(stop_signal.wait(), timeout=self.interval)
                except TimeoutError:
                    continue
        finally:
            if self._active:
                await asyncio.gather(
                    *(task for task, _ in self._active.values()),
                    return_exceptions=True,
                )
                self._finish_completed()

    def run_once(self, *, now: datetime | None = None) -> tuple[ClaimedJob, ...]:
        current = now or datetime.now(timezone.utc)
        state = self.get_agent_state()
        jobs = AgentJobs.merge(self.get_home_jobs(), state.program)
        claimed_jobs: list[ClaimedJob] = []
        for kind in self.kinds:
            self.job_store.reconcile(jobs=jobs, kind=kind, now=current)
            while claimed := self.job_store.claim_due(
                jobs=jobs,
                kind=kind,
                run_id=self.executor.allocate_run_id(),
                now=current,
            ):
                if not claimed.definition.input.strip():
                    self.job_store.finish_run(
                        jobs=jobs,
                        run_id=claimed.run_id,
                        run_status="finished",
                        now=current,
                    )
                    continue
                task = self.executor.start(
                    RunRequest(
                        group=f"scheduler:{kind}",
                        origin=kind,
                        run_id=claimed.run_id,
                        thread_id=claimed.job.thread_id,
                        input=claimed.definition.input,
                        metadata={
                            "job": claimed.definition.run_metadata(),
                            "job_trigger": claimed.trigger,
                        },
                    ),
                    state,
                )
                self._active[claimed.run_id] = (task, jobs)
                claimed_jobs.append(claimed)
        return tuple(claimed_jobs)

    def _finish_completed(self, *, now: datetime | None = None) -> None:
        current = now or datetime.now(timezone.utc)
        for run_id, (task, jobs) in tuple(self._active.items()):
            if not task.done():
                continue
            self._active.pop(run_id, None)
            try:
                run = task.result()
            except Exception:
                status = "failed"
            else:
                status = run.status
            self.job_store.finish_run(
                jobs=jobs,
                run_id=run_id,
                run_status=status,
                now=current,
            )
