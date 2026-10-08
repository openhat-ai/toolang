"""Live canonical-stream adapters and SSE encoding."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncGenerator, Callable

from fastapi import Request
from fastapi.sse import ServerSentEvent

from toolang.execution.errors import StreamOverflowError
from toolang.execution.events import (
    RunEnd,
    RunEvent,
    RunRetried,
    ThreadEvent,
    event_to_data,
)
from toolang.execution.stream import CanonicalStream, StreamReader

RUN_ID_HEADER = "X-Toolang-Run-ID"
KEEP_ALIVE_SEC = 15.0
LiveEvent = RunEvent | ThreadEvent


class EventSubscription:
    """Adapt bounded canonical batches to the existing live-only HTTP contract."""

    def __init__(self, reader: StreamReader) -> None:
        self._reader = reader
        self._pending: deque[LiveEvent] = deque()

    async def _next(self) -> LiveEvent:
        while not self._pending:
            batch = await self._reader.receive()
            # Retry invalidation and resume controls enter HTTP with normalization.
            self._pending.extend(
                frame.event
                for frame in batch.events
                if not isinstance(frame.event, RunRetried)
            )
        self._reader.check()
        return self._pending.popleft()

    async def receive(self, *, timeout: float) -> LiveEvent | None:
        try:
            return await asyncio.wait_for(self._next(), timeout=timeout)
        except TimeoutError:
            return None

    @property
    def empty(self) -> bool:
        self._reader.check()
        return not self._pending and self._reader.empty

    def close(self) -> None:
        self._pending.clear()
        self._reader.close()


class LiveEventRelay:
    """Select scopes from the executor's source without republishing events."""

    def __init__(self, source: CanonicalStream) -> None:
        self.source = source

    def subscribe_run(self, run_id: str) -> EventSubscription:
        return EventSubscription(self.source.subscribe(root_run_id=run_id))

    def subscribe_thread(self, thread_id: str) -> EventSubscription:
        return EventSubscription(self.source.subscribe(thread_id=thread_id))


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
                if subscription.empty:
                    return
            event = await subscription.receive(timeout=KEEP_ALIVE_SEC)
            if event is None:
                yield ServerSentEvent(comment="keep-alive")
                continue
            yield ServerSentEvent(
                event=event.type,
                data=event_to_data(event),
            )
            if (
                terminal_run_id is not None
                and isinstance(event, RunEnd)
                and event.run == terminal_run_id
            ):
                return
    except StreamOverflowError:
        yield ServerSentEvent(event="stream_error", data={"code": "overflow"})
    except StopAsyncIteration:
        return
    finally:
        subscription.close()


def _shutdown_started(request: Request) -> bool:
    signal = getattr(request.app.state, "shutdown_signal", None)
    return bool(signal is not None and signal.is_set())
