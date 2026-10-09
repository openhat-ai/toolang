"""Validate bounded agent publications before any shared backend mutation."""

from hashlib import sha256
import json
from typing import Any

from toolang.execution.errors import SnapshotLimitError
from toolang.execution.events import (
    RunBegin,
    RunEnd,
    StepBegin,
    StepEnd,
    ThreadCreated,
    ThreadForked,
    ThreadRewound,
    event_from_data,
)
from toolang.execution.observation import StreamScope
from toolang.execution.types import EventCursor
from .errors import EventProtocolError
from .events import MAX_EVENT_BYTES, encode
from .records import EventProjection, MAX_BYTES, MAX_ENTITIES, field
from .schemas import (
    CommitRequest,
    EventRequest,
    IncompleteRequest,
    RecoveryRequest,
    StagingRequest,
    stream_id,
)


def _key(key: str) -> tuple[str, str]:
    kind, identity = json.loads(key)
    if (
        kind not in {"run", "step", "root", "control"}
        or not isinstance(identity, str)
        or not identity
        or field(kind, identity) != key
    ):
        raise ValueError("Invalid entity key")
    return kind, identity


def _entities(values: dict[str, str]) -> dict[str, Any]:
    if len(values) > MAX_ENTITIES or len(encode(values).encode()) > MAX_BYTES:
        raise SnapshotLimitError("Projection exceeds upload budget")
    decoded = {}
    for key, raw in values.items():
        kind, identity = _key(key)
        value = json.loads(raw)
        if not isinstance(value, dict) or value.get("v") != 1:
            raise ValueError("Invalid entity version")
        if not isinstance(value.get("thread"), str):
            raise ValueError("Invalid entity thread")
        for name in ("source", "begin_source", "end_source"):
            if value.get(name) is not None:
                EventCursor.parse(value[name])
        for name in ("begin", "end", "event"):
            if value.get(name) is not None:
                event_from_data(value[name])
        if value.get("delivery") is not None:
            stream_id(value["delivery"])
        if kind == "root":
            if (
                value.get("root") != identity
                or type(value.get("terminal")) is not bool
                or not isinstance(value.get("completed_at"), str)
            ):
                raise ValueError("Invalid root entity")
        elif kind == "control":
            event = event_from_data(value["event"])
            if (
                not isinstance(event, ThreadCreated | ThreadForked | ThreadRewound)
                or str(event.control) != identity
                or value["thread"] != event.thread
                or value.get("root") is not None
            ):
                raise ValueError("Invalid control entity")
        else:
            begin = event_from_data(value["begin"])
            if not (
                kind == "run"
                and isinstance(begin, RunBegin)
                and begin.run == identity
                and begin.thread_id == value["thread"]
                or kind == "step"
                and isinstance(begin, StepBegin)
                and str(begin.step) == identity
            ):
                raise ValueError("Invalid entity Begin")
            run = begin.run if isinstance(begin, RunBegin) else begin.step.run_id
            if (
                value.get("run") != run
                or not isinstance(value.get("root"), str)
                or "begin_source" not in value
                or "end_source" not in value
            ):
                raise ValueError("Invalid entity structure")
            if value.get("end") is not None:
                end = event_from_data(value["end"])
                if not (
                    isinstance(begin, RunBegin)
                    and isinstance(end, RunEnd)
                    and begin.run == end.run
                    or isinstance(begin, StepBegin)
                    and isinstance(end, StepEnd)
                    and begin.step == end.step
                    and begin.kind == end.kind
                ):
                    raise ValueError("Invalid entity End")
        decoded[key] = value
    return decoded


def validate_publication(body: CommitRequest | StagingRequest, *, agent: str) -> None:
    try:
        if isinstance(body, StagingRequest):
            EventCursor.parse(body.source)
            entities = _entities(body.entities)
            if sha256(encode(body.entities).encode()).hexdigest() != body.digest:
                raise ValueError("Snapshot digest mismatch")
            EventProjection(entities).snapshot(StreamScope())
        elif isinstance(body, IncompleteRequest):
            if body.id != f"{body.recovery}:incomplete":
                raise ValueError("Invalid recovery identity")
        else:
            source = EventCursor.parse(body.source)
            if source.epoch != body.source_epoch:
                raise ValueError("Source epoch mismatch")
            if isinstance(body, RecoveryRequest):
                if body.id != f"{body.recovery}:commit":
                    raise ValueError("Invalid recovery identity")
            elif isinstance(body, EventRequest):
                prior = EventCursor.parse(body.prior)
                if (
                    body.id != body.source
                    or prior.epoch != source.epoch
                    or prior.seq >= source.seq
                ):
                    raise ValueError("Invalid source progression")
                if (
                    body.count > MAX_ENTITIES
                    or body.bytes > MAX_BYTES
                    or len(body.data.encode()) + len(agent.encode()) + 133
                    > MAX_EVENT_BYTES
                ):
                    raise SnapshotLimitError("Event publication exceeds budget")
                data = json.loads(body.data)
                if (
                    set(data) != {"v", "source_cursor", "thread", "root", "event"}
                    or data["v"] != 1
                    or data["source_cursor"] != body.source
                ):
                    raise ValueError("Invalid canonical envelope")
                event_from_data(data["event"])
                _entities(body.updates)
                if len(body.removed) > MAX_ENTITIES:
                    raise SnapshotLimitError("Too many removed entities")
                for key in body.removed:
                    _key(key)
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise EventProtocolError("Invalid publication") from exc
