"""Execution events and caller-facing observation contracts."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal, cast

from pydantic import Field, TypeAdapter

from toolang.base.types.message import Delta, MessagePart, MessagePartType

from .records import ThreadPeer, local_from_data, local_to_data
from .types import (
    ControlRef,
    ExecutionError,
    Local,
    RunStatus,
    StepKind,
    StepPath,
    StepStatus,
    ValuePtr,
)


@dataclass(frozen=True, slots=True)
class RunBegin:
    """A run started executing."""

    run: str
    control: ControlRef
    runnable: str = ""
    parent: StepPath | None = None
    placement: dict[str, object] | None = None
    started_at: str = ""
    type: Literal["run_begin"] = field(default="run_begin", init=False)


@dataclass(frozen=True, slots=True)
class StepBegin:
    """A run step started executing."""

    step: StepPath
    kind: StepKind
    input: tuple[ValuePtr, ...] = ()
    placement: dict[str, object] | None = None
    given: dict[str, Any] = field(default_factory=dict)
    started_at: str = ""
    type: Literal["step_begin"] = field(default="step_begin", init=False)


@dataclass(frozen=True, slots=True)
class PartBegin:
    """A streamed step part started."""

    step: StepPath
    part: int
    part_type: MessagePartType
    type: Literal["part_begin"] = field(default="part_begin", init=False)


@dataclass(frozen=True, slots=True)
class PartDelta:
    """A streamed step part produced a delta."""

    step: StepPath
    part: int
    delta: Delta
    type: Literal["part_delta"] = field(default="part_delta", init=False)


@dataclass(frozen=True, slots=True)
class PartEnd:
    """A streamed step part completed."""

    step: StepPath
    part: int
    data: MessagePart
    type: Literal["part_end"] = field(default="part_end", init=False)


@dataclass(frozen=True, slots=True)
class StepEnd:
    """A run step reached a terminal state."""

    step: StepPath
    kind: StepKind
    status: StepStatus
    output: Local | None = None
    noted: dict[str, Any] = field(default_factory=dict)
    error: ExecutionError | None = None
    finished_at: str = ""
    type: Literal["step_end"] = field(default="step_end", init=False)


@dataclass(frozen=True, slots=True)
class RunEnd:
    """A run reached a terminal state."""

    run: str
    status: RunStatus
    control: ControlRef | None = None
    output: Local | None = None
    error: ExecutionError | None = None
    finished_at: str = ""
    type: Literal["run_end"] = field(default="run_end", init=False)


RunEvent = Annotated[
    RunBegin | StepBegin | PartBegin | PartDelta | PartEnd | StepEnd | RunEnd,
    Field(discriminator="type"),
]


class RunTracer(ABC):
    """Observe the ordered run tree started by one caller."""

    @abstractmethod
    async def on_event(self, event: RunEvent) -> None:
        """Observe one run event after durable projection."""


@dataclass(frozen=True, slots=True)
class ThreadCreated:
    """A thread was created successfully."""

    thread: str
    control: ControlRef
    origin: str
    peer: ThreadPeer
    created_at: str
    type: Literal["thread_created"] = field(default="thread_created", init=False)


@dataclass(frozen=True, slots=True)
class ThreadForked:
    """A thread was forked successfully."""

    thread: str
    control: ControlRef
    source_thread: str
    anchor_run: str
    created_at: str
    type: Literal["thread_forked"] = field(default="thread_forked", init=False)


@dataclass(frozen=True, slots=True)
class ThreadRewound:
    """A thread was rewound successfully."""

    thread: str
    control: ControlRef
    anchor_run: str
    ejected_runs: tuple[str, ...]
    created_at: str
    type: Literal["thread_rewound"] = field(default="thread_rewound", init=False)


ThreadEvent = Annotated[
    ThreadCreated | ThreadForked | ThreadRewound,
    Field(discriminator="type"),
]
ExecutionEvent = RunEvent | ThreadEvent

_RUN_EVENT_ADAPTER = TypeAdapter(RunEvent)
_EXECUTION_EVENT_ADAPTER = TypeAdapter(ExecutionEvent)


def event_to_data(event: ExecutionEvent) -> dict[str, Any]:
    """Serialize one canonical execution event for a protocol boundary."""

    data = cast(
        dict[str, Any],
        _EXECUTION_EVENT_ADAPTER.dump_python(event, mode="json"),
    )
    return _with_canonical_output(event, data)


def run_event_to_data(event: RunEvent) -> dict[str, Any]:
    """Serialize one canonical run event."""

    data = cast(
        dict[str, Any],
        _RUN_EVENT_ADAPTER.dump_python(event, mode="json"),
    )
    return _with_canonical_output(event, data)


def run_event_from_data(data: object) -> RunEvent:
    """Parse one canonical run event."""

    if isinstance(data, dict):
        payload = cast(dict[str, Any], data)
    else:
        payload = None
    if payload is not None and payload.get("type") in {"step_end", "run_end"}:
        output = payload.get("output")
        if isinstance(output, dict):
            payload = {**payload, "output": local_from_data(output)}
        data = payload
    return _RUN_EVENT_ADAPTER.validate_python(data)


class ThreadListener(ABC):
    """Observe committed thread lifecycle events."""

    @abstractmethod
    def on_event(self, event: ThreadEvent) -> None:
        """Observe one successful thread mutation."""


def _with_canonical_output(
    event: ExecutionEvent,
    data: dict[str, Any],
) -> dict[str, Any]:
    if isinstance(event, StepEnd | RunEnd) and event.output is not None:
        return {**data, "output": local_to_data(event.output)}
    return data
