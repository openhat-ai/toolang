"""Queue-backed runner and run request types."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
import logging
from typing import TYPE_CHECKING, Any, Literal, cast

from toolang.base.types.message import Message, message_text
from .db import utc_now
from .events import RunEnd, RunStarting, RunSteering, RunStopping
from .records import CommandMode
from ..state.live import LiveState

if TYPE_CHECKING:
    from ..up import UptimeContext
    from .records import RunRecord
    from .response import ResponseSink

DEFAULT_GROUP_LIMITS: dict[str, int] = {
    "chat": 100,
    "pulse": 1,
    "pulse:chore": 2,
    "pulse:task": 4,
    "poll": 1,
    "file": 10,
    "hook": 1,
}

_LOGGER = logging.getLogger("toolang.run")


@dataclass(frozen=True, slots=True)
class RunRequest:
    """One queued run request."""

    group: str
    origin: str
    run_id: str | None = None
    thunk: str = ""
    message: Message | None = None
    thunk_name: str | None = None
    thread_id: str | None = None
    thread_kind: str | None = None
    model_selector: str | None = None
    model_selectors: tuple[str, ...] = ()
    tool_selectors: tuple[str, ...] | None = None
    cap_selectors: tuple[str, ...] = ()
    run_loop: str = "basic"
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
    log_path: str | None = None

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
        self._waiting_requests: dict[int, RunSubmission] = {}
        self._active_requests: dict[int, RunSubmission] = {}
        self._responses_by_run: dict[str, ResponseSink] = {}
        self._tasks_by_run: dict[str, asyncio.Task[RunOutcome]] = {}

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
            submission := RunSubmission(
                request=request,
                response=response,
                live=live,
                completion=completion,
            )
        )
        if request.run_id and response is not None:
            self._responses_by_run[request.run_id] = response
        self._ready.set()
        self._emit_run_starting(submission)
        self._emit_queue_event(
            "run_waiting",
            submission,
            position=len(self._pending),
            reason="queue",
        )
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

    def attach(self, context: UptimeContext) -> None:
        """Attach the runtime context used for execution and control commands."""

        self._context = context

    def spawn(self, context: UptimeContext) -> asyncio.Task[list[RunOutcome]]:
        """Attach one runtime context and drain in the background."""

        return asyncio.create_task(self.drain(context))

    async def drain(self, context: UptimeContext | None = None) -> list[RunOutcome]:
        if context is not None:
            self.attach(context)
        tasks: list[asyncio.Task[RunOutcome]] = []
        async with asyncio.TaskGroup() as task_group:
            while True:
                submission = await self._dequeue_submission()
                if submission is None:
                    break
                self._waiting_requests[id(submission)] = submission
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

        return tuple(
            item.request for item in (*self._pending, *self._waiting_requests.values())
        )

    def active_requests(self) -> tuple[RunRequest, ...]:
        """Return a snapshot of currently executing requests."""

        return tuple(item.request for item in self._active_requests.values())

    def notify_run_control(self, *, run_id: str, payload: dict[str, Any]) -> None:
        """Publish one accepted run control command."""

        event = self._command_event(payload)
        if event is not None:
            self._emit_response_trace_event(run_id=run_id, event=event)
            context = self._context
            if context is not None:
                context.events.publish_trace(event)
            return
        self._emit_response_event(run_id=run_id, event_type=str(payload.get("type") or ""), payload=payload)
        context = self._context
        if context is None:
            return
        event_type = str(payload.get("type") or "")
        context.events.publish(domain="run", domain_id=run_id, type=event_type, payload=payload)
        thread_id = payload.get("thread_id")
        if isinstance(thread_id, str) and thread_id:
            context.events.publish(domain="thread", domain_id=thread_id, type=event_type, payload=payload)

    def cancel_run(self, *, run_id: str, error: str | None = None) -> RunRecord:
        """Cancel one active run and notify its response sink."""

        context = self._context
        if context is None:
            raise RuntimeError("runner context is not attached")
        run = context.store.cancel_run(run_id=run_id, error=error)
        event = RunEnd(
            run_id=run.run_id,
            thread_id=run.thread_id,
            status="canceled",
            error=run.error,
            finished_at=run.finished_at or utc_now(),
        )
        self._emit_response_trace_event(run_id=run.run_id, event=event)
        context.events.publish_trace(event)
        self._cancel_task(run.run_id)
        return run

    async def _run_request(self, submission: RunSubmission) -> RunOutcome:
        request = submission.request
        request_key = id(submission)
        current_task = asyncio.current_task()
        if request.run_id and current_task is not None:
            self._tasks_by_run[request.run_id] = current_task
        try:
            if await self._group_is_full(request.group):
                self._emit_queue_event(
                    "run_waiting",
                    submission,
                    reason="group",
                )
            semaphore = await self._semaphore_for_group(request.group)
            async with semaphore:
                self._waiting_requests.pop(request_key, None)
                await self._update_group_in_flight(request.group, delta=1)
                self._active_requests[request_key] = submission
                try:
                    result = await self._execute_thread_locked(submission)
                    if submission.completion is not None and not submission.completion.done():
                        submission.completion.set_result(result)
                    self._completed.append(result)
                    return result
                except asyncio.CancelledError:
                    result = self._cancel_submission(submission)
                    if submission.completion is not None and not submission.completion.done():
                        submission.completion.set_result(result)
                    self._completed.append(result)
                    return result
                except Exception as exc:
                    result = self._fail_submission(submission, exc)
                    if submission.completion is not None and not submission.completion.done():
                        submission.completion.set_result(result)
                    self._completed.append(result)
                    return result
                finally:
                    self._active_requests.pop(request_key, None)
                    self._forget_response(submission)
                    self._forget_task(submission)
                    await self._update_group_in_flight(request.group, delta=-1)
        finally:
            self._waiting_requests.pop(request_key, None)
            self._forget_task(submission)

    async def _execute_thread_locked(self, submission: RunSubmission) -> RunOutcome:
        request = submission.request
        if request.thread_id is None:
            return await self._execute(submission)
        lock = await self._lock_for_thread(request.thread_id)
        if lock.locked():
            self._emit_queue_event(
                "run_waiting",
                submission,
                reason="thread",
            )
        async with lock:
            return await self._execute(submission)

    async def _execute(self, submission: RunSubmission) -> RunOutcome:
        from .execute import execute_run

        if self._context is None:
            raise RuntimeError("runner context is not attached")
        context = self._context
        request = submission.request
        delay_sec = self._delay_sec if request.delay_sec is None else request.delay_sec
        return await execute_run(
            context,
            submission,
            delay_sec=delay_sec,
            sleep=self._sleep,
        )

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

    async def _group_is_full(self, group: str) -> bool:
        async with self._group_lock:
            limit = self._group_limits.get(group, self._default_group_limit)
            return self._group_in_flight.get(group, 0) >= limit

    def _emit_queue_event(
        self,
        event_type: str,
        submission: RunSubmission,
        *,
        reason: str,
        position: int | None = None,
    ) -> None:
        request = submission.request
        payload = {
            "type": event_type,
            "run_id": request.run_id,
            "thread_id": request.thread_id,
            "origin": request.origin,
            "group": request.group,
            "request_id": _request_id(request),
            "executable_kind": _request_executable_kind(request),
            "executable_name": request.thunk_name,
            "reason": reason,
            "position": position,
            "created_at": utc_now(),
        }
        if submission.response is not None:
            on_queue_event = getattr(submission.response, "on_queue_event", None)
            if callable(on_queue_event):
                on_queue_event(event_type, payload)
        context = self._context
        if context is None:
            return
        run_id = request.run_id
        thread_id = request.thread_id or ""
        if run_id:
            context.events.publish(domain="run", domain_id=run_id, type=event_type, payload=payload)
        if thread_id:
            context.events.publish(domain="thread", domain_id=thread_id, type=event_type, payload=payload)

    def _emit_response_event(self, *, run_id: str, event_type: str, payload: dict[str, Any]) -> None:
        response = self._response_for_run(run_id)
        if response is None:
            return
        on_queue_event = getattr(response, "on_queue_event", None)
        if callable(on_queue_event):
            try:
                on_queue_event(event_type, payload)
            except Exception:
                _LOGGER.exception("response sink event handling failed")

    def _emit_response_trace_event(self, *, run_id: str, event: RunStarting | RunSteering | RunStopping | RunEnd) -> None:
        response = self._response_for_run(run_id)
        if response is None:
            return
        try:
            response.on_event(event)
        except Exception:
            _LOGGER.exception("response sink event handling failed")

    def _emit_run_starting(self, submission: RunSubmission) -> None:
        request = submission.request
        if not request.run_id:
            return
        event = RunStarting(
            run_id=request.run_id,
            origin=request.origin,
            thread_id=request.thread_id,
            input=request.message or Message.user(request.thunk),
            request_id=_request_id(request),
            accepted_at=utc_now(),
        )
        self._emit_response_trace_event(run_id=request.run_id, event=event)
        context = self._context
        if context is not None:
            context.events.publish_trace(event)

    def _command_event(self, payload: dict[str, Any]) -> RunSteering | RunStopping | None:
        kind = payload.get("kind")
        run_id = payload.get("run_id")
        thread_id = payload.get("thread_id")
        ref = payload.get("ref")
        index = ref.get("index") if isinstance(ref, dict) else payload.get("index")
        if not isinstance(run_id, str) or not run_id or not isinstance(thread_id, str) or not thread_id:
            return None
        try:
            command_index = int(0 if index is None else index)
        except (TypeError, ValueError):
            command_index = 0
        mode = payload.get("mode")
        mode_value = _command_mode(mode)
        request_id = payload.get("request_id")
        request_id_value = str(request_id) if request_id is not None else None
        accepted_at = str(payload.get("created_at") or utc_now())
        if kind == "steer":
            message = payload.get("message")
            if not isinstance(message, dict):
                return None
            return RunSteering(
                run_id=run_id,
                thread_id=thread_id,
                index=command_index,
                message=Message.from_data(message),
                mode=mode_value,
                request_id=request_id_value,
                accepted_at=accepted_at,
            )
        if kind == "stop":
            reason = payload.get("reason")
            return RunStopping(
                run_id=run_id,
                thread_id=thread_id,
                index=command_index,
                mode=mode_value,
                request_id=request_id_value,
                reason=str(reason) if reason is not None else None,
                accepted_at=accepted_at,
            )
        return None

    def _response_for_run(self, run_id: str) -> ResponseSink | None:
        return self._responses_by_run.get(run_id) or next(
            (
                submission.response
                for submission in self._active_requests.values()
                if submission.request.run_id == run_id and submission.response is not None
            ),
            None,
        )

    def _forget_response(self, submission: RunSubmission) -> None:
        run_id = submission.request.run_id
        if not run_id or submission.response is None:
            return
        if self._responses_by_run.get(run_id) is submission.response:
            self._responses_by_run.pop(run_id, None)

    def _cancel_task(self, run_id: str) -> None:
        task = self._tasks_by_run.get(run_id)
        if task is not None and not task.done():
            task.cancel()

    def _forget_task(self, submission: RunSubmission) -> None:
        run_id = submission.request.run_id
        if not run_id:
            return
        task = self._tasks_by_run.get(run_id)
        if task is not None and task is asyncio.current_task():
            self._tasks_by_run.pop(run_id, None)

    def _fail_submission(self, submission: RunSubmission, exc: Exception) -> RunOutcome:
        request = submission.request
        error = str(exc) or type(exc).__name__
        _LOGGER.exception("Run request failed before completion run=%s group=%s", request.run_id, request.group)
        run_id = request.run_id or ""
        thread_id = request.thread_id or ""
        if submission.response is not None and run_id:
            submission.response.on_event(
                RunEnd(
                    run_id=run_id,
                    thread_id=thread_id,
                    status="failed",
                    error=error,
                    finished_at=utc_now(),
                )
            )
        return RunOutcome(
            run_id=run_id,
            group=request.group,
            origin=request.origin,
            input_text=_request_input_text(request),
            thunk_name=request.thunk_name,
            thread_id=thread_id,
            delay_sec=0.0,
            status="failed",
            error=error,
        )

    def _cancel_submission(self, submission: RunSubmission) -> RunOutcome:
        request = submission.request
        return RunOutcome(
            run_id=request.run_id or "",
            group=request.group,
            origin=request.origin,
            input_text=_request_input_text(request),
            thunk_name=request.thunk_name,
            thread_id=request.thread_id or "",
            delay_sec=0.0,
            status="failed",
            error="canceled",
        )

    def __len__(self) -> int:
        return len(self._pending) + len(self._waiting_requests)


def _request_id(request: RunRequest) -> str | None:
    value = request.metadata.get("request_id")
    return str(value) if value is not None else None


def _request_executable_kind(request: RunRequest) -> str:
    value = request.metadata.get("executable_kind")
    return str(value) if value is not None else "thunk"


def _request_input_text(request: RunRequest) -> str:
    if request.thunk:
        return request.thunk
    if request.message is None:
        return ""
    return message_text(request.message.parts)


def _command_mode(value: object) -> CommandMode | None:
    if value in {"immediate", "next_step", "next_call"}:
        return cast(CommandMode, value)
    return None
