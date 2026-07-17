"""Resource-scoped runtime event streaming."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import asdict
import json
from typing import TYPE_CHECKING, Any

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
from .records import EventDomain, EventRecord

if TYPE_CHECKING:
    from .store import RunStore


async def stream_events(
    store: RunStore,
    *,
    domain: EventDomain,
    domain_id: str,
    after: int | None = None,
) -> AsyncIterator[str]:
    """Yield durable events after one cursor, including events from other processes."""

    cursor = after
    while True:
        events = store.list_events(
            domain=domain,
            domain_id=domain_id,
            after=cursor,
            limit=500,
        )
        if not events:
            await asyncio.sleep(0.1)
            continue
        for event in events:
            cursor = event.seq
            yield _sse_event(event)
            if domain == "run" and event.type == "run_end":
                return


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
        "payload": trace_event_payload(event),
    }


def _sse_event(event: EventRecord) -> str:
    data = json.dumps(event_data(event), separators=(",", ":"))
    return f"id: {event.seq}\nevent: event\ndata: {data}\n\n"


def trace_event_payload(event: TraceEvent) -> dict[str, Any]:
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
def _delta_data(delta: TextDelta | ToolCallDelta) -> dict[str, Any]:
    if isinstance(delta, TextDelta):
        return {"type": "text", "text": delta.text}
    return {"type": "tool_call", "text": delta.text, "tool_call_id": delta.tool_call_id}
