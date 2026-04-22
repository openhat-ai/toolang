"""Queue-backed runner and run request types."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
import logging
from typing import TYPE_CHECKING, Any, Literal

from toolang.base.types.message import Message, message_summary
from ..state.live import LiveState

if TYPE_CHECKING:
    from ..up import UptimeContext
    from .response import ResponseSink

DEFAULT_GROUP_LIMITS: dict[str, int] = {
    "chat": 1,
    "pulse": 1,
    "poll": 1,
    "hook": 1,
}
_LOGGER = logging.getLogger("toolang.run")


@dataclass(frozen=True, slots=True)
class RunRequest:
    """One queued run request."""

    group: str
    origin: str
    thunk: str = ""
    message: Message | None = None
    thunk_name: str | None = None
    thread_id: str | None = None
    model_selector: str | None = None
    run_strategy: str = "basic"
    delay_sec: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RunSubmission:
    """One runner-bound request plus execution attachments."""

    request: RunRequest
    response: ResponseSink | None = field(default=None, compare=False, repr=False)
    live: LiveState | None = None
    completion: asyncio.Future["RunOutcome"] | None = field(
        default=None,
        compare=False,
        repr=False,
    )


RunOutcomeStatus = Literal["finished", "failed"]


@dataclass(frozen=True, slots=True)
class RunOutcome:
    """One completed runtime outcome."""

    run_id: str
    group: str
    origin: str
    input_text: str
    thunk_name: str | None
    thread_id: str | None
    delay_sec: float
    status: RunOutcomeStatus
    output_text: str = ""
    error: str | None = None
    live_fingerprint: str | None = None

class QueueRunner:
    """Run queued requests with per-group and per-thread limits."""

    def __init__(
        self,
        *,
        group_limits: dict[str, int] | None = None,
        default_group_limit: int = 1,
        delay_sec: float = 0.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._pending: deque[RunSubmission] = deque()
        self._ready = asyncio.Event()
        self._closed = False
        self._group_limits = dict(group_limits or DEFAULT_GROUP_LIMITS)
        self._default_group_limit = default_group_limit
        self._delay_sec = delay_sec
        self._sleep = sleep
        self._context: UptimeContext | None = None
        self._group_semaphores = {
            group: asyncio.Semaphore(limit) for group, limit in self._group_limits.items()
        }
        self._group_in_flight = {group: 0 for group in self._group_limits}
        self._group_lock = asyncio.Lock()
        self._thread_locks: dict[str, asyncio.Lock] = {}
        self._thread_locks_lock = asyncio.Lock()
        self._completed: list[RunOutcome] = []
        self._active_requests: dict[int, RunSubmission] = {}

    def enqueue(
        self,
        request: RunRequest,
        *,
        response: ResponseSink | None = None,
        live: LiveState | None = None,
        completion: asyncio.Future[RunOutcome] | None = None,
    ) -> int:
        """Queue one run request."""

        if self._closed:
            raise RuntimeError("run queue is closed")
        self._pending.append(
            RunSubmission(
                request=request,
                response=response,
                live=live,
                completion=completion,
            )
        )
        self._ready.set()
        return len(self._pending)

    async def dequeue(self) -> RunRequest | None:
        """Remove one queued run request."""

        submission = await self._dequeue_submission()
        return None if submission is None else submission.request

    async def _dequeue_submission(self) -> RunSubmission | None:
        """Remove one queued run submission."""

        while True:
            if self._pending:
                submission = self._pending.popleft()
                if not self._pending and not self._closed:
                    self._ready.clear()
                return submission
            if self._closed:
                return None
            await self._ready.wait()

    def peek(self) -> RunRequest | None:
        """Return the next queued run request without removing it."""

        return self._pending[0].request if self._pending else None

    def close(self) -> None:
        """Stop accepting new run requests."""

        if self._closed:
            return
        self._closed = True
        self._ready.set()

    def spawn(self, context: UptimeContext) -> asyncio.Task[list[RunOutcome]]:
        """Attach one runtime context and drain in the background."""

        return asyncio.create_task(self.drain(context))

    async def drain(self, context: UptimeContext | None = None) -> list[RunOutcome]:
        if context is not None:
            self._context = context
        tasks: list[asyncio.Task[RunOutcome]] = []
        async with asyncio.TaskGroup() as task_group:
            while True:
                submission = await self._dequeue_submission()
                if submission is None:
                    break
                tasks.append(task_group.create_task(self._run_request(submission)))
        return [task.result() for task in tasks]

    def snapshot(self) -> dict[str, object]:
        """Return a lightweight snapshot of group and thread state."""

        live = self._context.live if self._context is not None else None
        return {
            "live_fingerprint": live.fingerprint if live is not None else None,
            "tracked_threads": len(self._thread_locks),
            "concurrency_groups": [
                {
                    "group": group,
                    "limit": self._group_limits.get(group, self._default_group_limit),
                    "in_flight": self._group_in_flight.get(group, 0),
                    "available": max(
                        self._group_limits.get(group, self._default_group_limit)
                        - self._group_in_flight.get(group, 0),
                        0,
                    ),
                }
                for group in sorted(self._group_semaphores)
            ],
            "completed_runs": len(self._completed),
        }

    def completed(self) -> list[RunOutcome]:
        """Return completed fake runs in completion order."""

        return list(self._completed)

    def pending_requests(self) -> tuple[RunRequest, ...]:
        """Return a snapshot of currently queued requests."""

        return tuple(item.request for item in self._pending)

    def active_requests(self) -> tuple[RunRequest, ...]:
        """Return a snapshot of currently executing requests."""

        return tuple(item.request for item in self._active_requests.values())

    async def _run_request(self, submission: RunSubmission) -> RunOutcome:
        request = submission.request
        semaphore = await self._semaphore_for_group(request.group)
        async with semaphore:
            await self._update_group_in_flight(request.group, delta=1)
            request_key = id(submission)
            self._active_requests[request_key] = submission
            try:
                result = await self._execute_thread_locked(submission)
                if submission.completion is not None and not submission.completion.done():
                    submission.completion.set_result(result)
                self._completed.append(result)
                return result
            except Exception as exc:
                if submission.completion is not None and not submission.completion.done():
                    submission.completion.set_exception(exc)
                raise
            finally:
                self._active_requests.pop(request_key, None)
                await self._update_group_in_flight(request.group, delta=-1)

    async def _execute_thread_locked(self, submission: RunSubmission) -> RunOutcome:
        request = submission.request
        if request.thread_id is None:
            return await self._execute(submission)
        lock = await self._lock_for_thread(request.thread_id)
        async with lock:
            return await self._execute(submission)

    async def _execute(self, submission: RunSubmission) -> RunOutcome:
        from .execute import execute_run

        if self._context is None:
            raise RuntimeError("runner context is not attached")
        context = self._context
        request = submission.request
        delay_sec = self._delay_sec if request.delay_sec is None else request.delay_sec
        input_summary = request.thunk
        if request.message is not None:
            input_summary = message_summary(request.message.parts) or input_summary
        _LOGGER.info(
            "starting run group=%s origin=%s thread_id=%s input=%r",
            request.group,
            request.origin,
            request.thread_id or "-",
            input_summary,
        )
        result = await execute_run(
            context,
            submission,
            delay_sec=delay_sec,
            sleep=self._sleep,
        )
        _LOGGER.info(
            "finished run run_id=%s group=%s origin=%s thread_id=%s status=%s",
            result.run_id[:12],
            result.group,
            result.origin,
            result.thread_id or "-",
            result.status,
        )
        return result

    async def _semaphore_for_group(self, group: str) -> asyncio.Semaphore:
        async with self._group_lock:
            semaphore = self._group_semaphores.get(group)
            if semaphore is None:
                limit = self._group_limits.get(group, self._default_group_limit)
                self._group_limits[group] = limit
                self._group_semaphores[group] = asyncio.Semaphore(limit)
                self._group_in_flight[group] = 0
                semaphore = self._group_semaphores[group]
            return semaphore

    async def _lock_for_thread(self, thread_id: str) -> asyncio.Lock:
        async with self._thread_locks_lock:
            lock = self._thread_locks.get(thread_id)
            if lock is None:
                lock = asyncio.Lock()
                self._thread_locks[thread_id] = lock
            return lock

    async def _update_group_in_flight(self, group: str, *, delta: int) -> None:
        async with self._group_lock:
            current = self._group_in_flight.get(group, 0)
            self._group_in_flight[group] = max(current + delta, 0)

    def __len__(self) -> int:
        return len(self._pending)
