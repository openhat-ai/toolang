"""Resource-scoped runtime event streaming."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import asdict
import json
import threading
from typing import TYPE_CHECKING, Any

from toolang.base.types.message import TextDelta, ToolCallDelta, parts_to_data

from .events import PartDelta, PartEnd, RunEnd, RunStart, StepEnd, StepStart, TraceEvent
from .records import EventDomain, EventRecord

if TYPE_CHECKING:
    from .db import ExecutionStore


class RuntimeEventBus:
    """Persist and fan out resource-scoped runtime events."""

    def __init__(self, store: "ExecutionStore", *, agent_id: str | None = None) -> None:
        self._store = store
        self._agent_id = agent_id
        self._lock = threading.Lock()
        self._subscribers: dict[tuple[EventDomain, str], list[_Subscription]] = {}

    def publish(
        self,
        *,
        domain: EventDomain,
        domain_id: str,
        type: str,
        payload: dict[str, Any],
    ) -> EventRecord:
        """Persist one event and broadcast it to live subscribers."""

        event = self._store.append_event(
            domain=domain,
            domain_id=domain_id,
            type=type,
            payload=payload,
        )
        key = (domain, domain_id)
        with self._lock:
            subscribers = tuple(self._subscribers.get(key, ()))
        for subscriber in subscribers:
            subscriber.put(event)
        return event

    def publish_trace(self, event: TraceEvent) -> None:
        """Publish the public event projection for one execution trace event."""

        event_type = _trace_event_type(event)
        payload = _trace_event_payload(event)
        run_id = payload.get("run_id")
        thread_id = payload.get("thread_id")
        if isinstance(run_id, str) and run_id:
            self.publish(domain="run", domain_id=run_id, type=event_type, payload=payload)
            if isinstance(event, RunStart):
                self.publish(
                    domain="run",
                    domain_id=run_id,
                    type="run_input",
                    payload=_run_input_payload(event),
                )
        if self._agent_id and isinstance(event, (RunStart, RunEnd)):
            agent_payload = dict(payload)
            if isinstance(event, RunStart):
                agent_payload["status"] = "running"
            self.publish(domain="agent", domain_id=self._agent_id, type="thread_update", payload=agent_payload)
        if isinstance(event, (RunStart, RunEnd)) and isinstance(thread_id, str) and thread_id:
            self.publish(domain="thread", domain_id=thread_id, type=event_type, payload=payload)
            if isinstance(event, RunStart):
                self.publish(
                    domain="thread",
                    domain_id=thread_id,
                    type="run_input",
                    payload=_run_input_payload(event),
                )

    async def stream(
        self,
        *,
        domain: EventDomain,
        domain_id: str,
        after: int | None = None,
    ) -> AsyncIterator[str]:
        """Yield historical events after the cursor, then live SSE frames."""

        for event in self._store.list_events(domain=domain, domain_id=domain_id, after=after, limit=500):
            yield _sse_event(event)
            if domain == "run" and event.type == "run_end":
                return

        subscription = _Subscription()
        key = (domain, domain_id)
        with self._lock:
            self._subscribers.setdefault(key, []).append(subscription)
        try:
            while True:
                event = await subscription.get()
                yield _sse_event(event)
                if domain == "run" and event.type == "run_end":
                    return
        finally:
            with self._lock:
                subscribers = self._subscribers.get(key)
                if subscribers is not None and subscription in subscribers:
                    subscribers.remove(subscription)
                if subscribers == []:
                    self._subscribers.pop(key, None)


class _Subscription:
    def __init__(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._queue: asyncio.Queue[EventRecord] = asyncio.Queue()

    async def get(self) -> EventRecord:
        return await self._queue.get()

    def put(self, event: EventRecord) -> None:
        self._loop.call_soon_threadsafe(self._queue.put_nowait, event)


def event_data(event: EventRecord) -> dict[str, Any]:
    """Return public event data."""

    return {
        "id": event.event_id,
        "cursor": event.seq,
        "domain": event.domain,
        "domain_id": event.domain_id,
        "type": event.type,
        "event_type": event.type,
        "at": event.created_at,
        "payload": dict(event.payload),
    }


def _sse_event(event: EventRecord) -> str:
    data = json.dumps(event_data(event), separators=(",", ":"))
    return f"id: {event.seq}\nevent: event\ndata: {data}\n\n"


def _trace_event_type(event: TraceEvent) -> str:
    return event.type.replace("-", "_")


def _trace_event_payload(event: TraceEvent) -> dict[str, Any]:
    payload = asdict(event)
    payload["type"] = _trace_event_type(event)
    if isinstance(event, RunStart):
        payload["input"] = event.input.to_data()
    elif isinstance(event, StepStart):
        payload.pop("instructions", None)
        payload["input"] = [asdict(item) if not hasattr(item, "to_data") else item.to_data() for item in event.input]
    elif isinstance(event, PartDelta):
        payload["delta"] = _delta_data(event.delta)
    elif isinstance(event, PartEnd):
        payload["part"] = parts_to_data((event.data,))[0]
        payload.pop("data", None)
    elif isinstance(event, StepEnd):
        payload["output"] = parts_to_data(event.output)
        payload["payload"] = event.payload.to_data()
    return payload


def _run_input_payload(event: RunStart) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "run_id": event.run_id,
        "thread_id": event.thread_id,
        "ref": {"kind": "input", "index": 0},
        "action": "start",
        "message": event.input.to_data(),
        "created_at": event.created_at,
        "type": "run_input",
    }
    if event.request_id is not None:
        payload["request_id"] = event.request_id
    return payload


def _delta_data(delta: TextDelta | ToolCallDelta) -> dict[str, Any]:
    if isinstance(delta, TextDelta):
        return {"type": "text", "text": delta.text}
    return {"type": "tool_call", "text": delta.text, "tool_call_id": delta.tool_call_id}
