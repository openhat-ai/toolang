"""Live event relay and canonical SSE helpers."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Callable
from dataclasses import asdict, dataclass
import threading
from typing import Any, Literal

from fastapi import Request
from fastapi.sse import ServerSentEvent

from toolang.execution.events import (
    PartBegin,
    RunBegin,
    RunEnd,
    RunEvent,
    RunTracer,
    ThreadEvent,
    ThreadForked,
    ThreadListener,
)

KEEP_ALIVE_SEC = 15.0
LiveEvent = RunEvent | ThreadEvent
LiveEventKind = Literal["run", "thread"]


@dataclass(frozen=True, slots=True)
class _Subscriber:
    loop: asyncio.AbstractEventLoop
    queue: asyncio.Queue[LiveEvent]


class EventSubscription:
    """One immediately registered live event subscription."""

    def __init__(
        self,
        relay: LiveEventRelay,
        *,
        kind: LiveEventKind,
        key: str,
    ) -> None:
        self._relay = relay
        self._kind = kind
        self._key = key
        self._subscriber = _Subscriber(
            loop=asyncio.get_running_loop(),
            queue=asyncio.Queue(),
        )
        self._closed = False
        relay._add(kind, key, self._subscriber)

    async def receive(self, *, timeout: float) -> LiveEvent | None:
        """Return the next event, or None when the keep-alive interval elapses."""

        try:
            return await asyncio.wait_for(
                self._subscriber.queue.get(),
                timeout=timeout,
            )
        except TimeoutError:
            return None

    @property
    def empty(self) -> bool:
        return self._subscriber.queue.empty()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._relay._remove(self._kind, self._key, self._subscriber)


class LiveEventRelay(ThreadListener):
    """Fan out process-local run and thread events to API subscribers."""

    def __init__(self) -> None:
        self._runs: dict[str, set[_Subscriber]] = {}
        self._threads: dict[str, set[_Subscriber]] = {}
        self._lock = threading.Lock()

    def trace(self, *, thread_id: str) -> RunTracer:
        """Create one tracer that publishes a newly started run tree."""

        return _RelayRunTracer(self, thread_id=thread_id)

    def subscribe_run(self, run_id: str) -> EventSubscription:
        return EventSubscription(self, kind="run", key=run_id)

    def subscribe_thread(self, thread_id: str) -> EventSubscription:
        return EventSubscription(self, kind="thread", key=thread_id)

    def on_event(self, event: ThreadEvent) -> None:
        """Publish one committed thread mutation from any caller thread."""

        thread_ids = {event.thread}
        if isinstance(event, ThreadForked):
            thread_ids.add(event.source_thread)
        self._publish(self._threads, thread_ids, event)

    def publish_run(
        self,
        event: RunEvent,
        *,
        root_run_id: str,
        thread_id: str,
    ) -> None:
        self._publish(self._runs, {root_run_id}, event)
        self._publish(self._threads, {thread_id}, event)

    def _add(
        self,
        kind: LiveEventKind,
        key: str,
        subscriber: _Subscriber,
    ) -> None:
        collection = self._runs if kind == "run" else self._threads
        with self._lock:
            collection.setdefault(key, set()).add(subscriber)

    def _remove(
        self,
        kind: LiveEventKind,
        key: str,
        subscriber: _Subscriber,
    ) -> None:
        collection = self._runs if kind == "run" else self._threads
        with self._lock:
            subscribers = collection.get(key)
            if subscribers is None:
                return
            subscribers.discard(subscriber)
            if not subscribers:
                collection.pop(key, None)

    def _publish(
        self,
        collection: dict[str, set[_Subscriber]],
        keys: set[str],
        event: LiveEvent,
    ) -> None:
        with self._lock:
            subscribers = {
                subscriber
                for key in keys
                for subscriber in collection.get(key, ())
            }
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None
        for subscriber in subscribers:
            if subscriber.loop is current_loop:
                subscriber.queue.put_nowait(event)
                continue
            try:
                subscriber.loop.call_soon_threadsafe(
                    subscriber.queue.put_nowait,
                    event,
                )
            except RuntimeError:
                continue


class _RelayRunTracer(RunTracer):
    def __init__(self, relay: LiveEventRelay, *, thread_id: str) -> None:
        self._relay = relay
        self._thread_id = thread_id
        self._root_run_id: str | None = None

    async def on_event(self, event: RunEvent) -> None:
        if self._root_run_id is None:
            if not isinstance(event, RunBegin):
                raise RuntimeError("run trace must begin with run_begin")
            self._root_run_id = event.run
        self._relay.publish_run(
            event,
            root_run_id=self._root_run_id,
            thread_id=self._thread_id,
        )


async def sse_stream(
    request: Request,
    subscription: EventSubscription,
    *,
    terminal_run_id: str | None = None,
    stopped: Callable[[], bool] | None = None,
) -> AsyncGenerator[ServerSentEvent, None]:
    """Yield canonical live events and transport-only keep-alive comments."""

    try:
        while True:
            if _shutdown_started(request):
                return
            if (
                stopped is not None
                and subscription.empty
                and await asyncio.to_thread(stopped)
            ):
                return
            event = await subscription.receive(timeout=KEEP_ALIVE_SEC)
            if event is None:
                yield ServerSentEvent(comment="keep-alive")
                continue
            yield ServerSentEvent(
                event=event.type,
                data=_event_data(event),
            )
            if (
                terminal_run_id is not None
                and isinstance(event, RunEnd)
                and event.run == terminal_run_id
            ):
                return
    finally:
        subscription.close()


def _event_data(event: LiveEvent) -> dict[str, Any]:
    data = asdict(event)
    if isinstance(event, PartBegin):
        data["part_type"] = data.pop("type_")
    return data


def _shutdown_started(request: Request) -> bool:
    signal = getattr(request.app.state, "shutdown_signal", None)
    return bool(signal is not None and signal.is_set())
