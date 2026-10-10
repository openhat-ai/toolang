"""Bounded structural event records for backend recovery."""

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from toolang.common.files import atomic_write_text
from .schemas import HubConnection

from toolang.execution.errors import SnapshotLimitError
from toolang.execution.events import (
    RunBegin,
    RunEnd,
    RunRetried,
    StepBegin,
    StepEnd,
    event_from_data,
    event_to_data,
)
from toolang.execution.observation import StreamScope, StructuralSnapshot
from toolang.execution.stream import CanonicalEvent
from toolang.execution.types import StepRef
from .errors import EventProtocolError, EventRecoveryRequired
from .events import PARTS, encode


class HubRecord(BaseModel):
    """Private discovery and process identity for a root's local Hub."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    pid: int = Field(gt=0)
    created: float = Field(gt=0)
    port: int = Field(ge=1, le=65535)
    human: str
    identity: str
    status: Literal["starting", "running"] = "running"

    @property
    def connection(self) -> HubConnection:
        return HubConnection(f"http://127.0.0.1:{self.port}", self.human, self.identity)

    def save(self, path: Path) -> None:
        if path.exists():
            path.chmod(0o600)
        atomic_write_text(path, self.model_dump_json() + "\n")

    @classmethod
    def load(cls, path: Path) -> HubRecord | None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return cls.model_validate(data)
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            raise ValueError(f"Invalid Hub record: {path}") from exc


MAX_ENTITIES = 10000
MAX_BYTES = 16 * 1024 * 1024
MAX_RECENT = 100


def field(kind: str, identity: str) -> str:
    return encode([kind, identity])


class EventProjection:
    """Absolute structural entities. Only the exporter mutates a candidate copy."""

    def __init__(self, entities: dict[str, dict[str, Any]] | None = None) -> None:
        self.entities = entities or {}

    def copy(self) -> EventProjection:
        # Nested events are immutable values; mutation replaces the enclosing entity.
        return EventProjection(
            {key: dict(value) for key, value in self.entities.items()}
        )

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
            if key != field(
                "run" if isinstance(begin, RunBegin) else "step", str(identity)
            ):
                raise EventProtocolError("Projection identity does not match its key")
            snapshot.begins[identity] = (begin, value["begin_source"])
            snapshot.children[parent].append(identity)
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
                    raise EventProtocolError("Projection End does not match its Begin")
                snapshot.ends[identity] = (end, value["end_source"])
        for children in snapshot.children.values():
            children.sort(
                key=lambda key: (
                    getattr(snapshot.begins[key][0], "started_at", "") or "",
                    str(key),
                )
            )
        # Validate the forest once. Walking every entity's ancestors is quadratic
        # for deep trees and never terminates when stored parent links form a cycle.
        pending = list(snapshot.children[None])
        seen: set[str | StepRef] = set()
        while pending:
            key = pending.pop()
            if key in seen:
                raise EventProtocolError("Projection contains a repeated entity")
            seen.add(key)
            pending.extend(snapshot.children.get(key, ()))
        if seen != snapshot.begins.keys():
            raise EventProtocolError("Projection contains orphaned or cyclic ancestry")
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
