"""Event-driven task and chore scheduling on a dedicated owner loop."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass
from datetime import datetime, timezone
import heapq
import itertools
import logging
import threading
from typing import Literal, TypeAlias, cast

from toolang.base.errors import ToolangError
from toolang.base.types.policy import RunBindings
from toolang.common.ids import IdIssuer
from toolang.common.layout import AgentLayout
from toolang.execution.calls import parse_call, resolve_spec
from toolang.execution.executor import RunExecutor, RunSpec
from toolang.execution.records import RunRecord
from toolang.lang.includes import resolve_file_include
from toolang.setup import AgentSetup
from toolang.state.state import AgentState

from .records import JobRecord
from .state import Job, merge_jobs, program_jobs
from .store import JobStore, next_activation
from .watcher import JobWatcher

DEFAULT_SAFETY_REFRESH_SECONDS = 30.0
DEFAULT_STATE_POLL_SECONDS = 1.0
_LOGGER = logging.getLogger(__name__)

_CommandKind: TypeAlias = Literal[
    "refresh",
    "snapshot",
    "reopen_task",
    "run_chore",
    "cancel_task",
    "get",
    "pause",
    "stop",
]


@dataclass(frozen=True, slots=True)
class _Command:
    kind: _CommandKind
    value: object = None
    result: Future[object] | None = None


class JobScheduler:
    """Own job checkpoints and timers outside the execution event loop."""

    def __init__(
        self,
        *,
        layout: AgentLayout,
        executor: RunExecutor,
        ids: IdIssuer,
        get_agent_setup: Callable[[], AgentSetup],
        get_agent_state: Callable[[], AgentState],
        safety_refresh_seconds: float = DEFAULT_SAFETY_REFRESH_SECONDS,
        state_poll_seconds: float = DEFAULT_STATE_POLL_SECONDS,
    ) -> None:
        self.layout = layout
        self.executor = executor
        self.ids = ids
        self.get_agent_setup = get_agent_setup
        self.get_agent_state = get_agent_state
        self.safety_refresh_seconds = safety_refresh_seconds
        self.state_poll_seconds = state_poll_seconds
        self._execution_loop: asyncio.AbstractEventLoop | None = None
        self._scheduler_loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._started: Future[object] | None = None
        self._commands: asyncio.Queue[_Command] | None = None
        self._watch_stop: asyncio.Event | None = None
        self._watcher: JobWatcher | None = None
        self._store: JobStore | None = None
        self._jobs: dict[str, Job] = {}
        self._records: dict[str, JobRecord] = {}
        self._tokens: dict[str, int] = {}
        self._heap: list[tuple[float, int, str, int]] = []
        self._sequence = itertools.count()
        self._active: dict[str, asyncio.Task[None]] = {}
        self._state_fingerprint = ""
        self._paused = False
        self._stopping = False

    async def start(self) -> None:
        """Start the dedicated scheduler thread from the execution loop."""

        if self._thread is not None:
            raise RuntimeError("job scheduler is already started")
        self._execution_loop = asyncio.get_running_loop()
        self._started = Future()
        self._thread = threading.Thread(
            target=self._thread_main,
            name=f"toolang-jobs-{self.layout.name}",
            daemon=True,
        )
        self._thread.start()
        await asyncio.wrap_future(self._started)

    async def refresh(self) -> None:
        """Refresh authored and program jobs on the scheduler loop."""

        await self._request("refresh")

    def refresh_sync(self) -> None:
        """Refresh jobs from a blocking API or CLI worker thread."""

        self._request_sync("refresh")

    async def reopen_task(self, task_id: str) -> JobRecord:
        """Request the current ready task revision again."""

        return _as_record(await self._request("reopen_task", task_id))

    def reopen_task_sync(self, task_id: str) -> JobRecord:
        """Reopen a task from a blocking caller thread."""

        return _as_record(self._request_sync("reopen_task", task_id))

    async def run_chore(self, chore_id: str) -> JobRecord:
        """Request one coalesced manual chore occurrence."""

        return _as_record(await self._request("run_chore", chore_id))

    def run_chore_sync(self, chore_id: str) -> JobRecord:
        """Request a manual chore run from a blocking caller thread."""

        return _as_record(self._request_sync("run_chore", chore_id))

    async def cancel_task(self, task_id: str) -> JobRecord:
        """Cancel a pending task or return its active checkpoint."""

        return _as_record(await self._request("cancel_task", task_id))

    def cancel_task_sync(self, task_id: str) -> JobRecord:
        """Cancel or inspect a task from a blocking caller thread."""

        return _as_record(self._request_sync("cancel_task", task_id))

    def get_sync(self, job_id: str) -> JobRecord | None:
        """Return one in-memory checkpoint to a blocking caller thread."""

        value = self._request_sync("get", job_id)
        if value is not None and not isinstance(value, JobRecord):
            raise TypeError("scheduler get did not return a JobRecord")
        return value

    async def get(self, job_id: str) -> JobRecord | None:
        """Return one in-memory checkpoint without blocking the caller loop."""

        value = await self._request("get", job_id)
        if value is not None and not isinstance(value, JobRecord):
            raise TypeError("scheduler get did not return a JobRecord")
        return value

    async def pause(self) -> None:
        """Stop new dispatch while retaining completion handling."""

        thread = self._thread
        loop = self._scheduler_loop
        if thread is None or not thread.is_alive() or loop is None:
            return
        try:
            await self._request("pause")
        except RuntimeError:
            if thread.is_alive() and loop.is_running():
                raise

    async def stop(self) -> None:
        """Stop and join the scheduler owner thread."""

        thread = self._thread
        if thread is None:
            return
        if thread.is_alive():
            loop = self._scheduler_loop
            if loop is not None and loop.is_running():
                try:
                    await self._request("stop")
                except RuntimeError:
                    if thread.is_alive() and loop.is_running():
                        raise
            await asyncio.to_thread(thread.join)
        self._thread = None
        self._scheduler_loop = None

    async def _request(self, kind: _CommandKind, value: object = None) -> object:
        return await asyncio.wrap_future(self._submit_request(kind, value))

    def _request_sync(self, kind: _CommandKind, value: object = None) -> object:
        return self._submit_request(kind, value).result()

    def _submit_request(
        self,
        kind: _CommandKind,
        value: object = None,
    ) -> Future[object]:
        loop = self._scheduler_loop
        queue = self._commands
        if loop is None or queue is None or not loop.is_running():
            raise RuntimeError("job scheduler is not running")
        result: Future[object] = Future()
        command = _Command(kind=kind, value=value, result=result)
        loop.call_soon_threadsafe(queue.put_nowait, command)
        return result

    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._scheduler_loop = loop
        try:
            loop.run_until_complete(self._serve())
        except BaseException as exc:
            started = self._started
            if started is not None and not started.done():
                started.set_exception(exc)
            else:
                _LOGGER.exception("job scheduler stopped unexpectedly")
        finally:
            loop.close()

    async def _serve(self) -> None:
        self._commands = asyncio.Queue()
        self._watch_stop = asyncio.Event()
        self._store = JobStore(self.layout.job_store)
        self._watcher = JobWatcher(self.layout)
        watch_task: asyncio.Task[None] | None = None
        try:
            self._reconcile(self._watcher.current())
            await self._recover_active()
            watch_task = asyncio.create_task(
                self._watch_updates(),
                name=f"toolang-job-watch-{self.layout.name}",
            )
            started = self._started
            if started is not None and not started.done():
                started.set_result(None)
            loop = asyncio.get_running_loop()
            next_safety = loop.time() + self.safety_refresh_seconds
            next_state = loop.time() + self.state_poll_seconds
            while not self._stopping:
                if not self._paused:
                    self._dispatch_due()
                now = loop.time()
                timeout = min(
                    self._heap_timeout(),
                    max(0.0, next_safety - now),
                    max(0.0, next_state - now),
                )
                try:
                    command = await asyncio.wait_for(
                        self._commands.get(),
                        timeout=timeout,
                    )
                except TimeoutError:
                    now = loop.time()
                    if now >= next_state:
                        next_state = now + self.state_poll_seconds
                        state = self.get_agent_state()
                        if state.fingerprint != self._state_fingerprint:
                            self._reconcile(self._watcher.current())
                    if now >= next_safety:
                        next_safety = now + self.safety_refresh_seconds
                        self._reconcile(self._watcher.refresh())
                    continue
                await self._handle(command)
        finally:
            if self._watch_stop is not None:
                self._watch_stop.set()
            if watch_task is not None:
                await asyncio.gather(watch_task, return_exceptions=True)
            if self._active:
                await asyncio.gather(
                    *tuple(self._active.values()),
                    return_exceptions=True,
                )
            if self._store is not None:
                self._store.close()
            self._store = None
            self._watcher = None

    async def _watch_updates(self) -> None:
        assert self._watcher is not None
        assert self._watch_stop is not None
        assert self._commands is not None
        try:
            async for snapshot in self._watcher.updates(
                stop_signal=self._watch_stop,
            ):
                await self._commands.put(_Command("snapshot", snapshot))
        except Exception as exc:
            _LOGGER.error("jobs.watch_failed error=%s", str(exc) or type(exc).__name__)

    async def _handle(self, command: _Command) -> None:
        try:
            value = await self._apply(command)
        except BaseException as exc:
            if command.result is not None and not command.result.done():
                command.result.set_exception(exc)
            elif isinstance(exc, Exception):
                _LOGGER.error(
                    "jobs.command_failed kind=%s error=%s",
                    command.kind,
                    str(exc) or type(exc).__name__,
                )
            else:
                raise
        else:
            if command.result is not None and not command.result.done():
                command.result.set_result(value)

    async def _apply(self, command: _Command) -> object:
        assert self._store is not None
        assert self._watcher is not None
        if command.kind == "refresh":
            self._reconcile(self._watcher.refresh())
            return None
        if command.kind == "snapshot":
            self._reconcile(_as_jobs(command.value))
            return None
        if command.kind in {"reopen_task", "run_chore", "cancel_task"}:
            self._reconcile(self._watcher.refresh())
            job_id = str(command.value)
            if job_id not in self._jobs:
                raise FileNotFoundError(f"ready job not found: {job_id}")
            if command.kind == "reopen_task":
                record = self._store.reopen_task(task_id=job_id)
            elif command.kind == "run_chore":
                record = self._store.request_manual_chore(chore_id=job_id)
            else:
                current = self._records.get(job_id)
                if current is None:
                    raise FileNotFoundError(f"ready job not found: {job_id}")
                record = (
                    current
                    if current.active_run_id is not None
                    else self._store.cancel_pending_task(task_id=job_id)
                )
            self._replace_record(record)
            return record
        if command.kind == "get":
            return self._records.get(str(command.value))
        if command.kind == "pause":
            self._paused = True
            if self._watch_stop is not None:
                self._watch_stop.set()
            return None
        if command.kind == "stop":
            self._paused = True
            self._stopping = True
            if self._watch_stop is not None:
                self._watch_stop.set()
            return None
        raise ValueError(f"unsupported scheduler command: {command.kind}")

    def _reconcile(self, authored: tuple[Job, ...]) -> None:
        assert self._store is not None
        state = self.get_agent_state()
        jobs = merge_jobs(authored, program_jobs(state.program))
        records = self._store.reconcile(jobs=jobs)
        self._jobs = jobs
        self._state_fingerprint = state.fingerprint
        self._set_records(records)

    def _set_records(self, records: tuple[JobRecord, ...]) -> None:
        previous_ids = set(self._records)
        self._records = {record.job_id: record for record in records}
        for job_id in previous_ids | set(self._records):
            self._invalidate(job_id)
        for job_id in self._records:
            self._push(job_id)

    async def _recover_active(self) -> None:
        assert self._store is not None
        for record in tuple(self._records.values()):
            run_id = record.active_run_id
            if run_id is None:
                continue
            run = await self._get_run(run_id)
            if run is None:
                restored = self._store.release_claim(run_id=run_id)
                if restored is not None:
                    self._replace_record(restored)
                continue
            if run.status in {"pending", "running"}:
                blocked = self._store.mark_recovery_blocked(
                    run_id=run_id,
                    error="accepted run was interrupted outside this executor",
                )
                if blocked is not None:
                    self._replace_record(blocked)
                _LOGGER.error("jobs.recovery_blocked run_id=%s", run_id)
                continue
            repaired = self._store.finish_run(
                jobs=self._jobs, run_id=run_id, run_status=run.status
            )
            if repaired is None:
                self._drop_record(record.job_id)
            else:
                self._replace_record(repaired)
        self._set_records(self._store.reconcile(jobs=self._jobs))

    async def _get_run(self, run_id: str) -> RunRecord | None:
        loop = self._execution_loop
        if loop is None:
            raise RuntimeError("execution loop is unavailable")

        async def get() -> RunRecord | None:
            return self.executor.store.get_run(run_id=run_id)

        future = asyncio.run_coroutine_threadsafe(get(), loop)
        return await asyncio.wrap_future(future)

    def _dispatch_due(self) -> None:
        assert self._store is not None
        now = datetime.now(timezone.utc)
        while self._heap:
            timestamp, _sequence, job_id, token = self._heap[0]
            if token != self._tokens.get(job_id):
                heapq.heappop(self._heap)
                continue
            if timestamp > now.timestamp():
                return
            heapq.heappop(self._heap)
            record = self._records.get(job_id)
            job = self._jobs.get(job_id)
            activation = next_activation(record) if record is not None else None
            if record is None or job is None or activation is None:
                continue
            due_at, trigger = activation
            if due_at > now:
                self._push(job_id)
                continue
            try:
                spec = self._build_spec(job)
            except (OSError, ToolangError, ValueError) as exc:
                error = str(exc) or type(exc).__name__
                _LOGGER.warning(
                    "jobs.submission_rejected kind=%s job_id=%s source=%s error=%s",
                    job.kind,
                    job.id,
                    job.source,
                    error,
                )
                rejected = self._store.reject_activation(
                    job=job,
                    trigger=trigger,
                    error=error,
                    now=now,
                )
                self._replace_record(rejected)
                continue
            run_id = self.ids.issue_run()
            claimed = self._store.claim(
                job=job,
                trigger=trigger,
                run_id=run_id,
                now=now,
            )
            self._replace_record(claimed.record)
            self._submit_run(run_id, job, spec)

    def _build_spec(self, job: Job) -> RunSpec:
        setup = self.get_agent_setup()
        state = self.get_agent_state()
        runnable = (
            job.kind
            if state.program.find_agic(job.kind) is not None
            or state.program.find_flow(job.kind) is not None
            else "default"
        )
        commands, input = parse_call(job.body)
        base = job.path.parent if job.path is not None else setup.layout.home
        spec = resolve_spec(
            commands,
            input,
            setup=setup,
            state=state,
            thread=job.thread_id,
            default_runnable=runnable,
            surface=RunBindings(runnable=runnable),
            include=lambda reference: resolve_file_include(reference, base=base),
        )
        return spec

    def _submit_run(self, run_id: str, job: Job, spec: RunSpec) -> None:
        execution_loop = self._execution_loop
        if execution_loop is None:
            raise RuntimeError("scheduler event loops are unavailable")

        async def execute() -> RunRecord:
            thread = self.executor.store.get_thread(thread_id=job.thread_id)
            if thread is None:
                self.executor.store.create_thread(
                    thread_id=job.thread_id,
                    origin=job.kind,
                )
            return await self.executor.start(spec, run_id=run_id)

        future = asyncio.run_coroutine_threadsafe(execute(), execution_loop)
        waiter = asyncio.create_task(
            self._wait_run(run_id, future),
            name=f"toolang-job-run-{run_id}",
        )
        self._active[run_id] = waiter

    async def _wait_run(
        self,
        run_id: str,
        future: Future[RunRecord],
    ) -> None:
        assert self._store is not None
        try:
            run = await asyncio.wrap_future(future)
        except Exception as exc:
            record = self._store.reject_claim(
                run_id=run_id,
                error=str(exc) or type(exc).__name__,
            )
        else:
            record = self._store.finish_run(
                jobs=self._jobs,
                run_id=run_id,
                run_status=run.status,
            )
        finally:
            self._active.pop(run_id, None)
        if record is None:
            previous = next(
                (
                    item
                    for item in self._records.values()
                    if item.active_run_id == run_id
                ),
                None,
            )
            if previous is not None:
                self._drop_record(previous.job_id)
        else:
            self._replace_record(record)

    def _replace_record(self, record: JobRecord) -> None:
        self._records[record.job_id] = record
        self._invalidate(record.job_id)
        self._push(record.job_id)

    def _drop_record(self, job_id: str) -> None:
        self._records.pop(job_id, None)
        self._invalidate(job_id)

    def _invalidate(self, job_id: str) -> None:
        self._tokens[job_id] = self._tokens.get(job_id, 0) + 1

    def _push(self, job_id: str) -> None:
        record = self._records.get(job_id)
        activation = next_activation(record) if record is not None else None
        if activation is None:
            return
        due_at, _trigger = activation
        heapq.heappush(
            self._heap,
            (
                due_at.timestamp(),
                next(self._sequence),
                job_id,
                self._tokens[job_id],
            ),
        )

    def _heap_timeout(self) -> float:
        while self._heap:
            timestamp, _sequence, job_id, token = self._heap[0]
            if token != self._tokens.get(job_id):
                heapq.heappop(self._heap)
                continue
            return max(0.0, timestamp - datetime.now(timezone.utc).timestamp())
        return max(self.safety_refresh_seconds, self.state_poll_seconds)


def _as_record(value: object) -> JobRecord:
    if not isinstance(value, JobRecord):
        raise TypeError("scheduler command did not return a JobRecord")
    return value


def _as_jobs(value: object) -> tuple[Job, ...]:
    if not isinstance(value, tuple) or not all(isinstance(item, Job) for item in value):
        raise TypeError("scheduler snapshot must contain Job values")
    return cast(tuple[Job, ...], value)
