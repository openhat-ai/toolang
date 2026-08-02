from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from toolang.base.types.message import Message
from toolang.base.types.run import ModelCallResult
from toolang.work.scheduler import Scheduler
from toolang.work.state import HomeJobs, JobDefinition
from toolang.work.store import JobStore
from tests.support.execution_harness import ExecutionHarness


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
_SOURCE = """
agic review(_: Part[], focus: Text):
  recall = none
  context: none
  instruct: none
  user: {{focus}} {{_}}
"""


def _task(body: str) -> JobDefinition:
    return JobDefinition(
        id="review",
        kind="task",
        name="review",
        title=None,
        body=body,
        source="tasks/review.md",
        path=None,
        schedule=None,
        fingerprint="task-review",
        thread="task_review",
    )


def test_scheduler_rejects_invalid_submission_without_creating_a_run(
    tmp_path,
) -> None:
    harness = ExecutionHarness.create(tmp_path / "agent", source=_SOURCE, responses=[])
    job_store = JobStore(tmp_path / "jobs.db")
    scheduler = Scheduler(
        job_store=job_store,
        executor=harness.executor,
        get_agent_setup=lambda: harness.setup,
        get_home_jobs=lambda: HomeJobs((_task(":help"),)),
        get_agent_state=lambda: harness.state,
    )
    try:
        assert len(scheduler.run_once(now=NOW)) == 1
        record = job_store.get(job_id="review", kind="task")
        assert record is not None
        assert record.status == "failed"
        assert record.failed_count == 1
        assert not harness.store.list_runs(limit=None)
        assert not harness.store.list_threads()
    finally:
        job_store.close()
        harness.store.close()


def test_scheduler_parses_and_binds_job_body_only_when_claimed(tmp_path) -> None:
    harness = ExecutionHarness.create(
        tmp_path / "agent",
        source=_SOURCE,
        responses=[ModelCallResult(message=Message.assistant("done"))],
    )
    job_store = JobStore(tmp_path / "jobs.db")
    scheduler = Scheduler(
        job_store=job_store,
        executor=harness.executor,
        get_agent_setup=lambda: harness.setup,
        get_home_jobs=lambda: HomeJobs(
            (_task(":agic review focus=security\n\nReview this."),)
        ),
        get_agent_state=lambda: harness.state,
    )

    async def scenario() -> None:
        assert len(scheduler.run_once(now=NOW)) == 1
        assert len(scheduler._active) == 1
        await asyncio.gather(*(task for task, _jobs in scheduler._active.values()))
        scheduler._finish_completed(now=NOW)

        runs = harness.store.list_runs(limit=None)
        assert len(runs) == 1
        assert runs[0].runnable_name == "review"
        control = harness.store.get_run_control(run_id=runs[0].id, index=0)
        assert control is not None
        assert control.input == Message.user("Review this.")
        record = job_store.get(job_id="review", kind="task")
        assert record is not None
        assert record.status == "done"

    try:
        asyncio.run(scenario())
    finally:
        job_store.close()
        harness.store.close()
