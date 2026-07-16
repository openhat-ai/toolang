"""Resource-scoped runtime event streaming."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import asdict
import json
import threading
from typing import Any

from toolang.base.types.message import TextDelta, ToolCallDelta, parts_to_data

from .events import (
    PartBegin,
    PartDelta,
    PartEnd,
    RunBegin,
    RunEnd,
    RunStarting,
    RunSteering,
    RunStopping,
    RunWaiting,
    StepBegin,
    StepEnd,
    TraceEvent,
)
from .records import EventDomain, EventRecord, trace_run
from .db import ExecutionStore, PersistSink


class RuntimeEventBus:
    """Persist and fan out resource-scoped runtime events."""

    def __init__(self, store: "ExecutionStore", *, agent_id: str | None = None) -> None:
        self._store = store
        self._persist = PersistSink(store)
        self._agent_id = agent_id
        self._lock = threading.Lock()
        self._trace_lock = threading.Lock()
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

        with self._trace_lock:
            self._publish_trace(event)

    def _publish_trace(self, event: TraceEvent) -> None:
        self._persist.on_event(event)
        payload = _trace_event_payload(event)
        run_id = payload.get("run")
        if not isinstance(run_id, str):
            step = payload.get("step")
            if isinstance(step, str) and step:
                run_id = trace_run(step)
        thread_id = payload.get("thread")
        if not isinstance(thread_id, str):
            thread_id = _event_context(payload).get("thread")
        if not isinstance(thread_id, str) and isinstance(run_id, str) and run_id:
            run = self._store.get_run(run_id=run_id)
            if run is not None:
                thread_id = run.thread
        if isinstance(run_id, str) and run_id:
            self.publish(
                domain="run", domain_id=run_id, type=event.type, payload=payload
            )
        if self._agent_id and isinstance(event, (RunBegin, RunEnd)):
            agent_payload = dict(payload)
            if isinstance(event, RunBegin):
                agent_payload["status"] = "running"
            self.publish(
                domain="agent",
                domain_id=self._agent_id,
                type="thread_update",
                payload=agent_payload,
            )
        if isinstance(thread_id, str) and thread_id:
            self.publish(
                domain="thread", domain_id=thread_id, type=event.type, payload=payload
            )

    async def stream(
        self,
        *,
        domain: EventDomain,
        domain_id: str,
        after: int | None = None,
    ) -> AsyncIterator[str]:
        """Yield historical events after the cursor, then live SSE frames."""

        for event in self._store.list_events(
            domain=domain, domain_id=domain_id, after=after, limit=500
        ):
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


def trace_event_data(event: TraceEvent) -> dict[str, Any]:
    """Return public stream data for one live trace event."""

    return {
        "type": event.type,
        "event_type": event.type,
        "payload": _trace_event_payload(event),
    }


def _sse_event(event: EventRecord) -> str:
    data = json.dumps(event_data(event), separators=(",", ":"))
    return f"id: {event.seq}\nevent: event\ndata: {data}\n\n"


def _trace_event_payload(event: TraceEvent) -> dict[str, Any]:
    payload = asdict(event)
    payload.pop("type", None)
    if isinstance(event, (RunWaiting, RunStarting)):
        payload["input"] = event.input.to_data()
    elif isinstance(event, RunSteering):
        payload["input"] = event.input.to_data()
    elif isinstance(event, RunStopping):
        if event.input is not None:
            payload["input"] = event.input.to_data()
    elif isinstance(event, RunBegin):
        payload["input"] = event.input.to_data()
    elif isinstance(event, StepBegin):
        payload["input"] = [
            asdict(item) if not hasattr(item, "to_data") else item.to_data()
            for item in event.input
        ]
    elif isinstance(event, PartBegin):
        payload["type"] = event.type_
        payload.pop("type_", None)
    elif isinstance(event, PartDelta):
        payload["delta"] = _delta_data(event.delta)
    elif isinstance(event, PartEnd):
        payload["data"] = parts_to_data((event.data,))[0]
    elif isinstance(event, StepEnd):
        payload["output"] = parts_to_data(event.output)
    elif isinstance(event, RunEnd):
        if event.input is not None:
            payload["input"] = event.input.to_data()
        if hasattr(event.output, "to_data"):
            payload["output"] = event.output.to_data()
    return payload


def _event_context(payload: dict[str, Any]) -> dict[str, Any]:
    context = payload.get("context")
    return dict(context) if isinstance(context, dict) else {}


def _delta_data(delta: TextDelta | ToolCallDelta) -> dict[str, Any]:
    if isinstance(delta, TextDelta):
        return {"type": "text", "text": delta.text}
    return {"type": "tool_call", "text": delta.text, "tool_call_id": delta.tool_call_id}
