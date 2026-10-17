"""Value-only notifications for execution subscribers.

Events describe committed observations. They are not persistence commands.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal, cast
from collections.abc import Mapping, Sequence

from pydantic import Field, TypeAdapter, ConfigDict
from toolang.lang.types import authored_type
from toolang.base.types.message import Delta, Part, PartType, part_from_data
from .records import occurrence_from_data, occurrence_to_data
from .types import (
    ControlRef,
    Occurrence,
    RunStatus,
    StepKind,
    StepStatus,
    AwaitableHandle,
    validate_occurrence,
)


FlowOperation = Literal[
    "let",
    "run",
    "spawn",
    "exec",
    "await",
    "seek",
    "ask",
    "generate",
    "reduce",
    "repeat",
    "map",
    "keep",
    "drop",
    "sort",
]


@dataclass(frozen=True, slots=True)
class FlowStepInfo:
    """Authored operation facts used to present a Flow Step, without its AST."""

    kind: FlowOperation
    runnable: str | None = None
    handle: str = ""
    name: str = ""
    binding: str | None = "_"
    doc: str | None = None
    asynchronous: bool = False
    count: int | None = None
    lanes: int | None = None
    position: Literal["first", "last"] | None = None
    order: Literal["ascending", "descending"] = "ascending"
    type: Literal["flow"] = field(default="flow", kw_only=True)


@dataclass(frozen=True, slots=True)
class ModelStepInfo:
    """Model identity; complete calls and recording revisions stay in records."""

    model: str
    type: Literal["model"] = field(default="model", kw_only=True)


@dataclass(frozen=True, slots=True)
class ToolStepInfo:
    """Tool identity and presentation facts, without invocation arguments."""

    plugin: str
    name: str
    summary: str = ""
    target: str = ""
    asynchronous: bool = False
    type: Literal["tool"] = field(default="tool", kw_only=True)


StepInfo = Annotated[
    FlowStepInfo | ModelStepInfo | ToolStepInfo, Field(discriminator="type")
]


@dataclass(frozen=True, slots=True)
class ControlApplied:
    """A control application correlated by Run identity and control index."""

    run: str
    index: int


@dataclass(frozen=True, slots=True)
class Usage:
    """Usage and selected USD cost needed for existing client summaries."""

    input_tokens: int
    output_tokens: int
    cache_read_tokens: int | None = None
    reasoning_tokens: int | None = None
    cost: float | None = None
    approximate: bool = True


@dataclass(frozen=True, slots=True)
class StepSummary:
    """Completion facts consumed by presenters, without accounting or provenance."""

    summary: str = ""
    runnable: str | None = None
    iterations: int | None = None
    termination: (
        Literal["exhausted", "satisfied", "exec", "failed", "canceled"] | None
    ) = None
    total: int | None = None
    total_items: int | None = None
    output_items: int | None = None
    usage: Usage | None = None


def _value_to_data(value: Any) -> Any:
    if isinstance(value, Part | AwaitableHandle):
        return _value_to_data(value.to_data())
    if isinstance(value, Mapping):
        return {str(key): _value_to_data(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        return [_value_to_data(item) for item in value]
    if value is None or isinstance(value, str | int | float | bool):
        return value
    raise TypeError(f"event output requires direct data, got {type(value).__name__}")


def _part_value(value: Any, kind: str) -> Any:
    """Keep canonical Parts at every dimension of an explicitly typed array."""
    if kind.endswith("[]"):
        if not isinstance(value, Sequence) or isinstance(value, str | bytes):
            raise ValueError(f"event {kind} output requires an array")
        return tuple(_part_value(item, kind[:-2]) for item in value)
    if isinstance(value, Part):
        return value
    if not isinstance(value, dict):
        raise ValueError("event Part output requires a canonical Part object")
    return part_from_data(value)


@dataclass(frozen=True, slots=True)
class EventOutput:
    """A concrete output value, without local bindings or pointer alternatives."""

    type: str
    value: Any

    def __post_init__(self) -> None:
        data = _value_to_data(self.value)
        kind = authored_type(self.type)
        if kind.split("[", 1)[0] == "Part":
            object.__setattr__(self, "value", _part_value(self.value, kind))
        elif self.type != "_Awaitable":
            object.__setattr__(self, "value", data)


@dataclass(frozen=True, slots=True)
class RunBegin:
    run: str
    control: int
    runnable: str = ""
    parent: str | None = None
    occurrence: Occurrence | None = None
    started_at: str = ""
    thread_id: str = ""
    type: Literal["run_begin"] = field(default="run_begin", kw_only=True)

    def __post_init__(self) -> None:
        validate_occurrence(self.occurrence)
        if type(self.control) is not int or self.control < 0:
            raise TypeError("run event control requires an index")
        if self.parent is not None and not isinstance(self.parent, str):
            raise TypeError("run event parent requires a scalar Step id")


@dataclass(frozen=True, slots=True)
class StepBegin:
    step: str
    run: str
    parent: str | None
    index: int
    kind: StepKind
    given: StepInfo
    preceded_by: tuple[ControlApplied, ...] = ()
    occurrence: Occurrence | None = None
    started_at: str = ""
    type: Literal["step_begin"] = field(default="step_begin", kw_only=True)

    def __post_init__(self) -> None:
        validate_occurrence(self.occurrence)
        if not isinstance(self.step, str) or not isinstance(
            self.given, FlowStepInfo | ModelStepInfo | ToolStepInfo
        ):
            raise TypeError("step event requires a scalar id and client Step info")
        if not all(isinstance(item, ControlApplied) for item in self.preceded_by):
            raise TypeError("step events require scalar control application facts")
        if (self.kind == "model") != isinstance(self.given, ModelStepInfo) or (
            self.kind == "tool"
        ) != isinstance(self.given, ToolStepInfo):
            raise TypeError("step event kind does not match client Step info")


@dataclass(frozen=True, slots=True)
class PartBegin:
    step: str
    part: int
    part_type: PartType
    type: Literal["part_begin"] = field(default="part_begin", kw_only=True)


@dataclass(frozen=True, slots=True)
class PartDelta:
    step: str
    part: int
    delta: Delta
    type: Literal["part_delta"] = field(default="part_delta", kw_only=True)


@dataclass(frozen=True, slots=True)
class PartEnd:
    step: str
    part: int
    data: Part
    type: Literal["part_end"] = field(default="part_end", kw_only=True)

    def __post_init__(self) -> None:
        _value_to_data(self.data)


@dataclass(frozen=True, slots=True)
class StepEnd:
    step: str
    run: str
    kind: StepKind
    status: StepStatus
    output: EventOutput | None = None
    noted: StepSummary | None = None
    error: str | None = None
    error_source: str | None = None
    finished_at: str = ""
    type: Literal["step_end"] = field(default="step_end", kw_only=True)

    def __post_init__(self) -> None:
        if self.output is not None and not isinstance(self.output, EventOutput):
            raise TypeError("terminal event output requires EventOutput")
        if self.error is not None and not isinstance(self.error, str):
            raise TypeError("terminal event error requires direct text")
        if self.noted is not None and not isinstance(self.noted, StepSummary):
            raise TypeError("step event completion requires StepSummary")


@dataclass(frozen=True, slots=True)
class RunEnd:
    run: str
    status: RunStatus
    control: ControlApplied | None = None
    output: EventOutput | None = None
    error: str | None = None
    error_source: str | None = None
    finished_at: str = ""
    type: Literal["run_end"] = field(default="run_end", kw_only=True)

    def __post_init__(self) -> None:
        if self.output is not None and not isinstance(self.output, EventOutput):
            raise TypeError("terminal event output requires EventOutput")
        if self.error is not None and not isinstance(self.error, str):
            raise TypeError("terminal event error requires direct text")


RunEvent = Annotated[
    RunBegin | StepBegin | PartBegin | PartDelta | PartEnd | StepEnd | RunEnd,
    Field(discriminator="type"),
]


@dataclass(frozen=True, slots=True)
class RunSnapshot:
    events: tuple[RunEvent, ...]
    type: Literal["run_snapshot"] = field(default="run_snapshot", init=False)


RunObservation = RunEvent | RunSnapshot


class RunTracer(ABC):
    async def on_snapshot(self, events: tuple[RunEvent, ...]) -> None:
        for event in events:
            await self.on_event(event)

    @abstractmethod
    async def on_event(self, event: RunEvent) -> None:
        """Observe one notification after durable commit."""


@dataclass(frozen=True, slots=True)
class ThreadCreated:
    """A thread was created successfully."""

    thread: str
    control: ControlRef
    origin: str
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
    """Runs were removed from a Thread's logical history, not from storage."""

    thread: str
    control: ControlRef
    anchor_run: str
    ejected_runs: tuple[str, ...]
    created_at: str
    type: Literal["thread_rewound"] = field(default="thread_rewound", init=False)


ThreadEvent = Annotated[
    ThreadCreated | ThreadForked | ThreadRewound, Field(discriminator="type")
]
ExecutionEvent = RunEvent | ThreadEvent
for _event_type in (
    FlowStepInfo,
    ModelStepInfo,
    ToolStepInfo,
    ControlApplied,
    Usage,
    StepSummary,
    EventOutput,
    RunBegin,
    StepBegin,
    PartBegin,
    PartDelta,
    PartEnd,
    StepEnd,
    RunEnd,
):
    setattr(_event_type, "__pydantic_config__", ConfigDict(extra="forbid"))

_RUN_EVENT_ADAPTER = TypeAdapter(RunEvent)
_EXECUTION_EVENT_ADAPTER = TypeAdapter(ExecutionEvent)


def event_to_data(event: ExecutionEvent) -> dict[str, Any]:
    data = cast(
        dict[str, Any], _EXECUTION_EVENT_ADAPTER.dump_python(event, mode="json")
    )
    if isinstance(event, RunBegin | StepBegin):
        data["occurrence"] = occurrence_to_data(event.occurrence)
    if isinstance(event, RunEnd | StepEnd) and event.output is not None:
        data["output"] = {
            "type": event.output.type,
            "value": _value_to_data(event.output.value),
        }
    return data


def run_event_to_data(event: RunEvent) -> dict[str, Any]:
    return event_to_data(event)


def run_event_from_data(data: object) -> RunEvent:
    if not isinstance(data, dict):
        raise ValueError("event must be an object")
    payload: dict[str, Any] = {
        key: value
        for key, value in cast(dict[str, Any], data).items()
        if key not in {"cursor", "context", "agent", "source_cursor"}
    }
    if payload.get("type") in {"run_begin", "step_begin"}:
        payload["occurrence"] = occurrence_from_data(payload.get("occurrence"))
    if (
        payload.get("type") in {"run_end", "step_end"}
        and payload.get("output") is not None
    ):
        output: Any = payload["output"]
        if not isinstance(output, dict) or set(output) != {"type", "value"}:
            raise ValueError("event output requires type and value")
        kind, value = output["type"], output["value"]
        if not isinstance(kind, str):
            raise ValueError("event output type must be text")
        if kind == "_Awaitable":
            value = AwaitableHandle.from_data(value, type_name=kind)
        payload["output"] = EventOutput(kind, value)
    return _RUN_EVENT_ADAPTER.validate_python(payload)


def event_from_data(data: object) -> ExecutionEvent:
    if isinstance(data, dict) and cast(dict[str, Any], data).get("type") in {
        "thread_created",
        "thread_forked",
        "thread_rewound",
    }:
        return _EXECUTION_EVENT_ADAPTER.validate_python(data)
    return run_event_from_data(data)


class ThreadListener(ABC):
    @abstractmethod
    def on_event(self, event: ThreadEvent) -> None:
        """Observe one successful thread mutation."""
