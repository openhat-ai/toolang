"""Value-only notifications for execution subscribers.

Events describe committed observations. They are not persistence commands.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal, cast

from pydantic import Field, TypeAdapter, ConfigDict, JsonValue
from typing_extensions import TypedDict, NotRequired
from toolang.lang.types import authored_type
from toolang.base.types.message import Delta, PartType, ImageDetail, AudioFormat
from .types import (
    ControlRef,
    Occurrence,
    RunStatus,
    StepKind,
    StepStatus,
    validate_occurrence,
    TokenUsage,
    ThreadKind,
    ThreadRef,
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
    usage: TokenUsage | None = None
    cost: float | None = None
    cost_approximate: bool = False


class TextPartData(TypedDict):
    type: Literal["text", "reasoning"]
    text: str


class ImagePartData(TypedDict):
    type: Literal["image"]
    image_url: NotRequired[str]
    file_id: NotRequired[str]
    detail: ImageDetail
    filename: NotRequired[str]
    media_type: NotRequired[str]


class AudioPartData(TypedDict):
    type: Literal["audio"]
    data: str
    format: AudioFormat
    filename: NotRequired[str]
    media_type: NotRequired[str]
    transcript: NotRequired[str]


class DocumentPartData(TypedDict):
    type: Literal["document"]
    data: NotRequired[str]
    url: NotRequired[str]
    file_id: NotRequired[str]
    filename: NotRequired[str]
    media_type: NotRequired[str]


class ToolCallData(TypedDict):
    type: Literal["tool_call"]
    tool_call_id: str
    tool_name: str
    tool_family: str


class ToolResultData(TypedDict):
    type: Literal["tool_result"]
    tool_call_id: str
    tool_name: str
    tool_family: str
    error: NotRequired[str]
    run: NotRequired[str]
    thread: NotRequired[str]
    control_applied: NotRequired[bool]
    cwd: NotRequired[str]


PartData = Annotated[
    TextPartData
    | ImagePartData
    | AudioPartData
    | DocumentPartData
    | ToolCallData
    | ToolResultData,
    Field(discriminator="type"),
]
for _part_type in (
    TextPartData,
    ImagePartData,
    AudioPartData,
    DocumentPartData,
    ToolCallData,
    ToolResultData,
):
    setattr(_part_type, "__pydantic_config__", ConfigDict(extra="forbid"))

_PART_DATA_ADAPTER = TypeAdapter(PartData)
_JSON_ADAPTER = TypeAdapter(JsonValue)


def _validate_part_output(value: JsonValue, kind: str) -> None:
    if kind.endswith("[]"):
        if not isinstance(value, list):
            raise ValueError(f"event {kind} output requires an array")
        for item in value:
            _validate_part_output(item, kind[:-2])
    else:
        _PART_DATA_ADAPTER.validate_python(value, strict=True)


@dataclass(frozen=True, slots=True)
class EventOutput:
    """Final client data; no execution values, bindings or pointer alternatives."""

    type: str
    value: JsonValue

    def __post_init__(self) -> None:
        kind = authored_type(self.type)
        if kind.split("[", 1)[0] == "Part":
            _validate_part_output(self.value, kind)
        else:
            _JSON_ADAPTER.validate_python(self.value, strict=True)


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
    spec: StepInfo
    preceded_by: tuple[ControlApplied, ...] = ()
    occurrence: Occurrence | None = None
    started_at: str = ""
    type: Literal["step_begin"] = field(default="step_begin", kw_only=True)

    def __post_init__(self) -> None:
        validate_occurrence(self.occurrence)
        if not isinstance(self.step, str) or not isinstance(
            self.spec, FlowStepInfo | ModelStepInfo | ToolStepInfo
        ):
            raise TypeError("step event requires a scalar id and client Step info")
        if not all(isinstance(item, ControlApplied) for item in self.preceded_by):
            raise TypeError("step events require scalar control application facts")
        if (self.kind == "model") != isinstance(self.spec, ModelStepInfo) or (
            self.kind == "tool"
        ) != isinstance(self.spec, ToolStepInfo):
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
    data: PartData
    type: Literal["part_end"] = field(default="part_end", kw_only=True)

    def __post_init__(self) -> None:
        _PART_DATA_ADAPTER.validate_python(self.data, strict=True)


@dataclass(frozen=True, slots=True)
class StepEnd:
    step: str
    run: str
    kind: StepKind
    status: StepStatus
    output: EventOutput | None = None
    report: StepSummary | None = None
    error: str | None = None
    error_source: str | None = None
    finished_at: str = ""
    type: Literal["step_end"] = field(default="step_end", kw_only=True)

    def __post_init__(self) -> None:
        if self.output is not None and not isinstance(self.output, EventOutput):
            raise TypeError("terminal event output requires EventOutput")
        if self.error is not None and not isinstance(self.error, str):
            raise TypeError("terminal event error requires direct text")
        if self.report is not None and not isinstance(self.report, StepSummary):
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
    created_at: str
    type: Literal["thread_created"] = field(default="thread_created", init=False)

    @property
    def kind(self) -> ThreadKind:
        """Return the context kind encoded by the created Thread ID."""

        return ThreadRef(self.thread).kind


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
    TokenUsage,
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
    return cast(
        dict[str, Any], _EXECUTION_EVENT_ADAPTER.dump_python(event, mode="json")
    )


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
