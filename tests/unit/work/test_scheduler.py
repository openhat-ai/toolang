from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timezone
import threading

from toolang.base.types.message import Message
from toolang.base.types.run import ModelCallResult
from toolang.catalog.job import AuthoredJobs, JobFile
from toolang.execution.records import CreateControlPayload, StartControlPayload
from toolang.execution.types import Local
from toolang.work.authoring import new_job_file
from toolang.work.records import JobRecord
from toolang.work.scheduler import JobScheduler
from toolang.work.state import load_ready_jobs
from toolang.work.store import JobStore
from tests.support.execution_harness import (
    AsyncGate,
    ExecutionHarness,
    ScriptedModelTurn,
)


_SOURCE = """
agic review(_: Part[], focus: Text):
  recall = none
  context: none
  instruct: none
  user: {{focus}} {{_}}
"""


def _result(text: str = "done") -> ModelCallResult:
    return ModelCallResult(message=Message.assistant(text))


def _create_task(
    catalog: AuthoredJobs,
    job_id: str,
    body: str,
) -> JobFile:
    return catalog.create(
        new_job_file(
            kind="task",
            job_id=job_id,
            title=None,
            body=body,
        )
    )


def _scheduler(harness: ExecutionHarness) -> JobScheduler:
    return JobScheduler(
        layout=harness.setup.layout,
        executor=harness.executor,
        ids=harness.ids,
        get_agent_setup=lambda: harness.setup,
        get_agent_state=lambda: harness.state,
        safety_refresh_seconds=60.0,
        state_poll_seconds=60.0,
    )


async def _wait_record(
    scheduler: JobScheduler,
    job_id: str,
    predicate: Callable[[JobRecord], bool],
) -> JobRecord:
    async with asyncio.timeout(5):
        while True:
            record = await scheduler.get(job_id)
            if record is not None and predicate(record):
                return record
            await asyncio.sleep(0.01)


async def _close_scheduler(
    scheduler: JobScheduler,
    harness: ExecutionHarness,
) -> None:
    await scheduler.pause()
    await harness.executor.shutdown()
    await scheduler.stop()


def test_scheduler_rejects_invalid_input_without_creating_a_run(
    tmp_path,
) -> None:
    harness = ExecutionHarness.create(
        tmp_path / "toolang",
        source=_SOURCE,
        responses=[],
    )
    _create_task(AuthoredJobs(harness.setup.layout.home), "review", ":help")
    scheduler = _scheduler(harness)

    async def scenario() -> None:
        try:
            await scheduler.start()
            record = await _wait_record(
                scheduler,
                "review",
                lambda item: item.status == "failed",
            )

            assert record.active_run_id is None
            assert record.error is not None
            assert "primary input must escape a leading colon" in record.error
            assert not harness.store.list_runs(limit=None)
            assert not harness.store.list_threads()
        finally:
            await _close_scheduler(scheduler, harness)

    try:
        asyncio.run(scenario())
    finally:
        harness.store.close()


def test_scheduler_submits_and_awaits_runs_on_the_execution_loop(
    tmp_path,
    monkeypatch,
) -> None:
    harness = ExecutionHarness.create(
        tmp_path / "toolang",
        source=_SOURCE,
        responses=[_result()],
    )
    _create_task(
        AuthoredJobs(harness.setup.layout.home),
        "review",
        ":agic review focus=security\n\nReview this.",
    )
    scheduler = _scheduler(harness)
    start_threads: list[int] = []
    original_start = harness.executor.start

    def record_start(*args, **kwargs):
        start_threads.append(threading.get_ident())
        return original_start(*args, **kwargs)

    monkeypatch.setattr(harness.executor, "start", record_start)

    async def scenario() -> None:
        execution_thread = threading.get_ident()
        try:
            await scheduler.start()
            record = await _wait_record(
                scheduler,
                "review",
                lambda item: item.status == "done",
            )

            assert record.active_run_id is None
            assert scheduler._thread is not None
            assert scheduler._thread.ident != execution_thread
            assert start_threads == [execution_thread]

            runs = harness.store.list_runs(limit=None)
            assert len(runs) == 1
            assert runs[0].thread == "task_review"
            control = harness.store.get_run_control(run_id=runs[0].id, index=0)
            assert control is not None
            assert isinstance(control.payload, StartControlPayload)
            assert control.payload.runnable == "agic:review"
            assert control.payload.locals == (
                Local.typed("Part[]", Message.user("Review this.").parts, "_"),
                Local.typed("Text", "security", "focus"),
            )
            created = harness.store.get_thread_control(
                thread_id="task_review",
                index=0,
            )
            assert created is not None
            assert created.payload == CreateControlPayload()
        finally:
            await _close_scheduler(scheduler, harness)

    try:
        asyncio.run(scenario())
    finally:
        harness.store.close()


def test_scheduler_recovers_a_claim_with_no_accepted_run(tmp_path) -> None:
    harness = ExecutionHarness.create(
        tmp_path / "toolang",
        source=_SOURCE,
        responses=[_result()],
    )
    _create_task(
        AuthoredJobs(harness.setup.layout.home),
        "review",
        ":agic review focus=recovery\n\nReview after recovery.",
    )
    (job,) = load_ready_jobs(harness.setup.layout)
    checkpoint = JobStore(harness.setup.layout.job_store)
    try:
        checkpoint.reconcile(
            jobs={job.id: job},
            now=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        checkpoint.claim(
            job=job,
            trigger="source",
            run_id="run_missing",
            now=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
    finally:
        checkpoint.close()
    scheduler = _scheduler(harness)

    async def scenario() -> None:
        try:
            await scheduler.start()
            await _wait_record(
                scheduler,
                "review",
                lambda item: item.status == "done",
            )

            runs = harness.store.list_runs(limit=None)
            assert len(runs) == 1
            assert runs[0].id != "run_missing"
        finally:
            await _close_scheduler(scheduler, harness)

    try:
        asyncio.run(scenario())
    finally:
        harness.store.close()


def test_scheduler_accepts_blocking_controls_from_api_worker_threads(
    tmp_path,
) -> None:
    harness = ExecutionHarness.create(
        tmp_path / "toolang",
        source=_SOURCE,
        responses=[_result("first"), _result("reopened")],
    )
    _create_task(
        AuthoredJobs(harness.setup.layout.home),
        "review",
        ":agic review focus=worker\n\nReview from a worker.",
    )
    scheduler = _scheduler(harness)

    async def scenario() -> None:
        try:
            await scheduler.start()
            await _wait_record(
                scheduler,
                "review",
                lambda item: item.status == "done",
            )

            reopened = await asyncio.to_thread(
                scheduler.reopen_task_sync,
                "review",
            )
            assert reopened.status == "pending"
            await _wait_record(
                scheduler,
                "review",
                lambda item: item.status == "done",
            )
            assert len(harness.store.list_runs(limit=None)) == 2
        finally:
            await _close_scheduler(scheduler, harness)

    try:
        asyncio.run(scenario())
    finally:
        harness.store.close()


def test_scheduler_serializes_revisions_of_one_task(tmp_path) -> None:
    gate = AsyncGate()
    harness = ExecutionHarness.create(
        tmp_path / "toolang",
        source=_SOURCE,
        responses=[
            ScriptedModelTurn(result=_result("first"), gate=gate),
            _result("second"),
        ],
    )
    catalog = AuthoredJobs(harness.setup.layout.home)
    authored = _create_task(
        catalog,
        "review",
        ":agic review focus=first\n\nReview the first revision.",
    )
    scheduler = _scheduler(harness)

    async def scenario() -> None:
        try:
            await scheduler.start()
            await gate.wait_until_entered()
            running = await _wait_record(
                scheduler,
                "review",
                lambda item: item.status == "running",
            )
            first_run = running.active_run_id
            assert first_run is not None

            catalog.update(
                authored.with_body(
                    ":agic review focus=second\n\nReview the second revision."
                )
            )
            await scheduler.refresh()
            edited = await scheduler.get("review")
            assert edited is not None
            assert edited.status == "running"
            assert edited.active_run_id == first_run
            assert len(harness.store.list_runs(limit=None)) == 1

            gate.release()
            await _wait_record(
                scheduler,
                "review",
                lambda item: item.status == "done",
            )
            async with asyncio.timeout(5):
                while len(harness.store.list_runs(limit=None)) != 2:
                    await asyncio.sleep(0.01)

            runs = harness.store.list_runs(limit=None)
            assert {run.thread for run in runs} == {"task_review"}
            assert len(harness.adapter.invocations) == 2
        finally:
            gate.release()
            await _close_scheduler(scheduler, harness)

    try:
        asyncio.run(scenario())
    finally:
        harness.store.close()


def test_scheduler_allows_different_jobs_to_run_concurrently(tmp_path) -> None:
    first_gate = AsyncGate()
    second_gate = AsyncGate()
    harness = ExecutionHarness.create(
        tmp_path / "toolang",
        source=_SOURCE,
        responses=[
            ScriptedModelTurn(result=_result("first"), gate=first_gate),
            ScriptedModelTurn(result=_result("second"), gate=second_gate),
        ],
    )
    catalog = AuthoredJobs(harness.setup.layout.home)
    _create_task(catalog, "alpha", ":agic review focus=alpha\n\nAlpha.")
    _create_task(catalog, "beta", ":agic review focus=beta\n\nBeta.")
    scheduler = _scheduler(harness)

    async def scenario() -> None:
        try:
            await scheduler.start()
            await asyncio.gather(
                first_gate.wait_until_entered(),
                second_gate.wait_until_entered(),
            )
            alpha, beta = await asyncio.gather(
                scheduler.get("alpha"),
                scheduler.get("beta"),
            )

            assert alpha is not None and alpha.status == "running"
            assert beta is not None and beta.status == "running"
            assert alpha.active_run_id != beta.active_run_id
            assert len(harness.adapter.invocations) == 2

            first_gate.release()
            second_gate.release()
            await asyncio.gather(
                _wait_record(
                    scheduler,
                    "alpha",
                    lambda item: item.status == "done",
                ),
                _wait_record(
                    scheduler,
                    "beta",
                    lambda item: item.status == "done",
                ),
            )
        finally:
            first_gate.release()
            second_gate.release()
            await _close_scheduler(scheduler, harness)

    try:
        asyncio.run(scenario())
    finally:
        harness.store.close()
