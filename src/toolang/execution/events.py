"""Execution events and caller-facing observation contracts."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal, cast

from pydantic import Field, TypeAdapter

from toolang.base.types.message import Delta, Part, PartType

from .records import (
    ThreadPeer,
    occurrence_from_data,
    occurrence_to_data,
    step_given_from_data,
    step_given_to_data,
    step_noted_from_data,
    step_noted_to_data,
)
from .types import (
    ControlRef,
    ErrorMessage,
    ErrorRef,
    FieldRef,
    Local,
    Occurrence,
    RunStatus,
    StepGiven,
    StepKind,
    StepNoted,
    StepRef,
    StepStatus,
    local_from_protocol_data,
    local_to_protocol_data,
    validate_occurrence,
    validate_step_given,
    validate_step_noted,
)


@dataclass(frozen=True, slots=True)
class RunBegin:
    """A run started executing."""

    run: str
    control: ControlRef
    runnable: str = ""
    parent: StepRef | None = None
    occurrence: Occurrence | None = None
    started_at: str = ""
    type: Literal["run_begin"] = field(default="run_begin", init=False)

    def __post_init__(self) -> None:
        validate_occurrence(self.occurrence)


@dataclass(frozen=True, slots=True)
class StepBegin:
    """A run step started executing."""

    step: StepRef
    kind: StepKind
    given: StepGiven
    state: ControlRef | None = None
    input: tuple[FieldRef, ...] = ()
    occurrence: Occurrence | None = None
    started_at: str = ""
    type: Literal["step_begin"] = field(default="step_begin", init=False)

    def __post_init__(self) -> None:
        validate_occurrence(self.occurrence)
        validate_step_given(self.kind, self.given)


@dataclass(frozen=True, slots=True)
class PartBegin:
    """A streamed step part started."""

    step: StepRef
    part: int
    part_type: PartType
    type: Literal["part_begin"] = field(default="part_begin", init=False)


@dataclass(frozen=True, slots=True)
class PartDelta:
    """A streamed step part produced a delta."""

    step: StepRef
    part: int
    delta: Delta
    type: Literal["part_delta"] = field(default="part_delta", init=False)


@dataclass(frozen=True, slots=True)
class PartEnd:
    """A streamed step part completed."""

    step: StepRef
    part: int
    data: Part
    type: Literal["part_end"] = field(default="part_end", init=False)


@dataclass(frozen=True, slots=True)
class StepEnd:
    """A run step reached a terminal state."""

    step: StepRef
    kind: StepKind
    status: StepStatus
    output: Local | None = None
    noted: StepNoted = None
    error: ErrorMessage | ErrorRef | None = None
    finished_at: str = ""
    type: Literal["step_end"] = field(default="step_end", init=False)

    def __post_init__(self) -> None:
        validate_step_noted(self.kind, self.noted, self.status)
        if self.error is not None and not isinstance(
            self.error, ErrorMessage | ErrorRef
        ):
            raise TypeError("step error requires ErrorMessage or ErrorRef")


@dataclass(frozen=True, slots=True)
class RunEnd:
    """A run reached a terminal state."""

    run: str
    status: RunStatus
    control: ControlRef | None = None
    output: Local | None = None
    error: ErrorMessage | ErrorRef | None = None
    finished_at: str = ""
    type: Literal["run_end"] = field(default="run_end", init=False)

    def __post_init__(self) -> None:
        if self.error is not None and not isinstance(
            self.error, ErrorMessage | ErrorRef
        ):
            raise TypeError("run error requires ErrorMessage or ErrorRef")


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
    return _with_canonical_fields(event, data)


def run_event_to_data(event: RunEvent) -> dict[str, Any]:
    """Serialize one canonical run event."""

    data = cast(
        dict[str, Any],
        _RUN_EVENT_ADAPTER.dump_python(event, mode="json"),
    )
    return _with_canonical_fields(event, data)


def run_event_from_data(data: object) -> RunEvent:
    """Parse one canonical run event."""

    payload = dict(cast(dict[str, Any], data)) if isinstance(data, dict) else None
    if payload is not None and "placement" in payload:
        raise ValueError("run events use occurrence instead of placement")
    if payload is not None and payload.get("type") in {"run_begin", "step_begin"}:
        payload["occurrence"] = occurrence_from_data(payload.get("occurrence"))
    if payload is not None and payload.get("type") == "step_begin":
        kind = _step_kind(payload.get("kind"))
        if kind is not None:
            payload["given"] = step_given_from_data(kind, payload.get("given"))
    if payload is not None and payload.get("type") in {"step_end", "run_end"}:
        output = payload.get("output")
        if isinstance(output, dict):
            payload["output"] = local_from_protocol_data(output)
    if payload is not None and payload.get("type") == "step_end":
        kind = _step_kind(payload.get("kind"))
        if kind is not None:
            payload["noted"] = step_noted_from_data(kind, payload.get("noted"))
    if payload is not None:
        data = payload
    return _RUN_EVENT_ADAPTER.validate_python(data)


class ThreadListener(ABC):
    """Observe committed thread lifecycle events."""

    @abstractmethod
    def on_event(self, event: ThreadEvent) -> None:
        """Observe one successful thread mutation."""


def _with_canonical_fields(
    event: ExecutionEvent,
    data: dict[str, Any],
) -> dict[str, Any]:
    if isinstance(event, RunBegin):
        data["occurrence"] = occurrence_to_data(event.occurrence)
    if isinstance(event, StepBegin):
        data["given"] = step_given_to_data(event.kind, event.given)
        data["occurrence"] = occurrence_to_data(event.occurrence)
    if isinstance(event, StepEnd):
        data["noted"] = step_noted_to_data(event.kind, event.noted)
    if isinstance(event, StepEnd | RunEnd) and event.output is not None:
        data["output"] = local_to_protocol_data(event.output)
    return data


def _step_kind(value: object) -> StepKind | None:
    if isinstance(value, str) and value in {
        "run",
        "agent",
        "human",
        "model",
        "tool",
        "par",
        "loop",
        "value",
    }:
        return cast(StepKind, value)
    return None
