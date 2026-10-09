"""Bounded, vendor-independent event projections and Hub observation vocabulary."""

from __future__ import annotations

from dataclasses import dataclass, replace
import json
from typing import Any
from uuid import UUID

from toolang.execution.events import (
    PartBegin,
    PartDelta,
    PartEnd,
    RunBegin,
    RunEnd,
    RunRetried,
    StepBegin,
    StepEnd,
    event_from_data,
    event_to_data,
)
from toolang.execution.observation import StructuralSnapshot
from toolang.execution.errors import SnapshotLimitError
from toolang.execution.stream import CanonicalEvent
from toolang.execution.subscriptions import StreamScope
from toolang.execution.types import StepRef
from .errors import EventProtocolError, EventRecoveryRequired
from .schemas import stream_id, target

MAX_ENTITIES = 10000
MAX_BYTES = 16 * 1024 * 1024
MAX_RECENT = 100
MAX_EVENT_BYTES = 1024 * 1024
MAX_STREAM_BYTES = 64 * 1024 * 1024
MAX_STREAM_EVENTS = 10000
PARTS = (PartBegin, PartDelta, PartEnd)


def encode(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


@dataclass(frozen=True, order=True)
class HubCursor:
    epoch: str
    position: tuple[int, int]

    def __str__(self) -> str:
        return f"h1.{self.epoch}.{self.position[0]}-{self.position[1]}"

    @property
    def stream_id(self) -> str:
        return f"{self.position[0]}-{self.position[1]}"

    @classmethod
    def parse(cls, value: str) -> HubCursor:
        try:
            version, epoch, sid = value.split(".")
            if version != "h1" or UUID(epoch).hex != epoch:
                raise ValueError
            return cls(epoch, stream_id(sid))
        except (TypeError, AttributeError, ValueError) as exc:
            raise ValueError("Invalid Hub cursor") from exc


@dataclass(frozen=True)
class HubScope:
    agent: str | None = None
    thread: str | None = None
    run: str | None = None

    def __post_init__(self) -> None:
        if self.agent is not None:
            target(self.agent, kind="agent")
        if (self.thread is not None or self.run is not None) and self.agent is None:
            raise ValueError("Thread/run scope requires an agent")
        if self.thread is not None and self.run is not None:
            raise ValueError("Select either thread or run")
        if self.thread == "" or self.run == "":
            raise ValueError("Empty scope")

    @property
    def local(self) -> StreamScope:
        return StreamScope(root=self.run, thread=self.thread)

    def data(self) -> dict[str, str]:
        if self.agent is None:
            return {"kind": "team"}
        return {**self.local.data(), "agent": self.agent}


def field(kind: str, identity: str) -> str:
    return encode([kind, identity])


class Projection:
    """Absolute structural entities. Only the exporter mutates a candidate copy."""

    def __init__(self, entities: dict[str, dict[str, Any]] | None = None) -> None:
        self.entities = entities or {}

    def copy(self) -> Projection:
        # Nested events are immutable values; mutation replaces the enclosing entity.
        return Projection({key: dict(value) for key, value in self.entities.items()})

    def apply(self, frame: CanonicalEvent) -> bool:
        event, cursor = frame.event, str(frame.cursor)
        if isinstance(event, PARTS):
            return False
        if isinstance(event, RunRetried):
            root_key = field("run", event.run)
            current = self.entities.get(root_key)
            if current is None:
                raise EventRecoveryRequired("Retry root is missing")
            begin = event_from_data(current["begin"])
            if not isinstance(begin, RunBegin):
                raise EventProtocolError("Invalid run entity")
            self.entities[root_key] = {
                **current,
                "begin": event_to_data(
                    replace(begin, control=event.control, started_at="")
                ),
                "begin_source": None,
                "end": None,
                "end_source": None,
                "delivery": None,
            }
            for run in event.removed_runs:
                self.entities.pop(field("run", run), None)
                for key, value in tuple(self.entities.items()):
                    if value.get("run") == run:
                        self.entities.pop(key)
            for ref in event.invalidated_steps:
                self.entities.pop(field("step", str(ref)), None)
        elif isinstance(event, RunBegin | RunEnd | StepBegin | StepEnd):
            is_run = isinstance(event, RunBegin | RunEnd)
            is_begin = isinstance(event, RunBegin | StepBegin)
            identity = (
                event.run if isinstance(event, RunBegin | RunEnd) else str(event.step)
            )
            key = field("run" if is_run else "step", identity)
            previous = self.entities.get(key)
            if not is_begin and previous is None:
                raise EventRecoveryRequired("End has no retained Begin")
            value = dict(
                previous
                or {
                    "v": 1,
                    "root": frame.root_run_id,
                    "thread": frame.thread_id,
                    "run": event.run
                    if isinstance(event, RunBegin | RunEnd)
                    else event.step.run_id,
                }
            )
            if is_begin:
                value.update(
                    begin=event_to_data(event),
                    begin_source=cursor,
                    end=None,
                    end_source=None,
                )
            else:
                value.update(end=event_to_data(event), end_source=cursor)
            value["delivery"] = None
            self.entities[key] = value
        else:
            self.entities[field("control", str(event.control))] = {
                "v": 1,
                "thread": frame.thread_id,
                "root": None,
                "event": event_to_data(event),
                "source": cursor,
                "delivery": None,
            }
        if frame.root_run_id is not None:
            self.entities[field("root", frame.root_run_id)] = {
                "v": 1,
                "root": frame.root_run_id,
                "thread": frame.thread_id,
                "source": cursor,
                "delivery": None,
                "completed_at": max(
                    (
                        value.get("end", {}).get("finished_at", "")
                        for key, value in self.entities.items()
                        if key.startswith('["run",')
                        and value["root"] == frame.root_run_id
                        and value.get("end")
                    ),
                    default="",
                ),
                "terminal": all(
                    value.get("end") is not None
                    for key, value in self.entities.items()
                    if key.startswith('["run",') and value["root"] == frame.root_run_id
                ),
            }
        return True

    def trim(self) -> bool:
        removed = False
        terminal = sorted(
            (
                (key, value)
                for key, value in self.entities.items()
                if key.startswith('["root",') and value["terminal"]
            ),
            key=lambda item: item[1].get("completed_at") or "",
        )

        def over() -> bool:
            return (
                len(self.entities) > MAX_ENTITIES
                or len(
                    encode(
                        {key: encode(value) for key, value in self.entities.items()}
                    ).encode()
                )
                + 128 * len(self.entities)
                > MAX_BYTES - 4096
            )

        while terminal and (len(terminal) > MAX_RECENT or over()):
            _, root = terminal.pop(0)
            self.entities = {
                key: value
                for key, value in self.entities.items()
                if value.get("root") != root["root"]
            }
            removed = True
        controls = [key for key in self.entities if key.startswith('["control",')]
        while controls and over():
            self.entities.pop(controls.pop(0))
            removed = True
        if over():
            raise SnapshotLimitError("Active projection exceeds budget")
        return removed

    def snapshot(
        self, scope: StreamScope, roots: set[str] | None = None
    ) -> StructuralSnapshot:
        snapshot = StructuralSnapshot()
        for key, value in self.entities.items():
            if "begin" not in value or (
                roots is not None and value["root"] not in roots
            ):
                continue
            if scope.root is not None and value["root"] != scope.root:
                continue
            if scope.thread is not None and value["thread"] != scope.thread:
                continue
            begin = event_from_data(value["begin"])
            if isinstance(begin, RunBegin):
                identity: str | StepRef = begin.run
                parent = begin.parent
                snapshot.parents[begin.run] = parent
            elif isinstance(begin, StepBegin):
                identity = begin.step
                parent = begin.step.parent or begin.step.run_id
            else:
                raise EventProtocolError("Invalid projection Begin")
            snapshot.begins[identity] = (begin, value["begin_source"])
            snapshot.children[parent].append(identity)
            if value.get("end") is not None:
                snapshot.ends[identity] = (
                    event_from_data(value["end"]),
                    value["end_source"],
                )
        for children in snapshot.children.values():
            children.sort(
                key=lambda key: (
                    getattr(snapshot.begins[key][0], "started_at", "") or "",
                    str(key),
                )
            )
        for key in snapshot.begins:
            begin = snapshot.begins[key][0]
            for ancestor in snapshot.ancestors(begin):
                if ancestor not in snapshot.begins:
                    raise EventProtocolError("Projection ancestor is missing")
        return snapshot

    def controls(self, scope: StreamScope) -> list[dict[str, Any]]:
        return [
            value
            for value in self.entities.values()
            if "event" in value
            and scope.root is None
            and (
                scope.thread is None
                or value["thread"] == scope.thread
                or (
                    value["event"]["type"] == "thread_forked"
                    and value["event"].get("source_thread") == scope.thread
                )
            )
        ]
