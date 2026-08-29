"""Caller-facing execution protocol schemas."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, fields, is_dataclass
import re
from types import UnionType
from typing import (
    Annotated,
    Any,
    Literal,
    Union,
    cast,
    get_args,
    get_origin,
    get_type_hints,
)

from pydantic import BaseModel, TypeAdapter

from toolang.base.types.message import Part, message_summary
from toolang.base.types.run import ModelCall
from toolang.lang.input import RunnableInputRaw
from toolang.lang.types import Array, Struct
from .records import (
    ControlPayloadField,
    PreparationControlPayload,
    ControlRecord,
    RunRecord,
    StepRecord,
    StoredModelStepGiven,
    ThreadPeer,
    ThreadRecord,
    stored_step_given_to_data,
    step_message_role,
)
from .types import (
    ControlRef,
    ControlTiming,
    ControlKind,
    ControlStatus,
    ExecutionError,
    Local,
    ModelStepGiven,
    Occurrence,
    RunStatus,
    StepKind,
    StepGiven,
    StepNoted,
    StepPath,
    StepStatus,
    ThreadPeerType,
    Pointer,
    RunOverride,
    TypedPointer,
    validate_execution_id,
    validate_occurrence,
    validate_step_given,
    validate_step_noted,
)
from .values import parts_from_local


Record = ThreadRecord | ControlRecord | RunRecord | StepRecord
_RECORD_TYPES: dict[str, type[Record]] = {
    "thread": ThreadRecord,
    "control": ControlRecord,
    "run": RunRecord,
    "step": StepRecord,
}
_RECORD_ADAPTERS = {
    kind: TypeAdapter(record_type) for kind, record_type in _RECORD_TYPES.items()
}
_ARRAY_ANNOTATION_RE = re.compile(r"^(?:tuple|list)\[(.+?)(?:, \.\.\.)?\]$")


@dataclass(frozen=True, slots=True)
class RecordSelection:
    """One canonical record or field selection with code-owned type metadata."""

    pointer: Pointer
    record: Record
    value: object
    runtime: object
    annotation: object
    type_name: str
    render_type: str

    @property
    def is_pointer(self) -> bool:
        """Return whether the selected field contains a domain Pointer."""

        return isinstance(self.runtime, Pointer | TypedPointer)

    def child(self, token: str | int) -> RecordSelection:
        """Select one direct child without another store lookup."""

        return select_record(self.record, self.pointer.select(token))


def record_kind(record: Record) -> Literal["thread", "control", "run", "step"]:
    """Return the public kind of one durable record."""

    if isinstance(record, ThreadRecord):
        return "thread"
    if isinstance(record, ControlRecord):
        return "control"
    if isinstance(record, RunRecord):
        return "run"
    if isinstance(record, StepRecord):
        return "step"
    raise TypeError(f"unsupported record: {type(record).__name__}")


def record_to_data(record: Record) -> dict[str, object]:
    """Serialize one record to its canonical public JSON document."""

    kind = record_kind(record)
    data = cast(
        dict[str, object],
        _RECORD_ADAPTERS[kind].dump_python(record, mode="json"),
    )
    if isinstance(record, StepRecord):
        data["given"] = stored_step_given_to_data(record.kind, record.given)
    if isinstance(record, RunRecord | StepRecord) and isinstance(record.error, Pointer):
        data["error"] = str(record.error)
    validate_field_names(data)
    return data


def select_record(record: Record, pointer: Pointer) -> RecordSelection:
    """Select a canonical record field and its current declared type."""

    kind = record_kind(record)
    if pointer.kind != kind:
        raise ValueError(f"Pointer identifies {pointer.kind}, not {kind}: {pointer}")
    data: object = record_to_data(record)
    runtime: object = record
    annotation: object = type(record)
    name = type(record).__name__
    render_type = name
    for token in pointer.tokens:
        data = _select_json_child(data, token, source=str(pointer))
        runtime, annotation, name, render_type = _select_runtime_child(
            runtime,
            annotation,
            token,
            data,
            source=str(pointer),
        )
    return RecordSelection(
        pointer=pointer,
        record=record,
        value=data,
        runtime=runtime,
        annotation=annotation,
        type_name=name,
        render_type=render_type,
    )


def validate_field_names(value: object) -> None:
    """Reject colon-bearing object member names at a durable record boundary."""

    if isinstance(value, Mapping):
        for name, child in value.items():
            if not isinstance(name, str):
                raise ValueError("record field names must be text")
            if ":" in name:
                raise ValueError(f"record field name cannot contain ':': {name!r}")
            validate_field_names(child)
        return
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for child in value:
            validate_field_names(child)


def _select_json_child(value: object, token: str, *, source: str) -> object:
    if isinstance(value, Mapping):
        mapping = cast(Mapping[str, object], value)
        if token not in mapping:
            raise ValueError(f"field does not exist ({token!r}): {source}")
        return mapping[token]
    if isinstance(value, list):
        if token == "-" or not _canonical_array_index(token):
            raise ValueError(f"invalid array index: {source}")
        index = int(token)
        if index >= len(value):
            raise ValueError(f"array index is out of range: {source}")
        return value[index]
    raise ValueError(f"field traverses a scalar: {source}")


def _select_runtime_child(
    runtime: object,
    annotation: object,
    token: str,
    data: object,
    *,
    source: str,
) -> tuple[object, object, str, str]:
    if isinstance(runtime, Local):
        return _select_local_child(runtime, token, data, source=source)
    if isinstance(runtime, Array | Sequence) and not isinstance(
        runtime, (str, bytes, bytearray)
    ):
        if not _canonical_array_index(token):
            return data, Any, "Json", "Json"
        index = int(token)
        child = runtime[index] if index < len(runtime) else data
        declared = _sequence_item_annotation(annotation)
        name = _declared_name(declared, declared)
        if isinstance(runtime, Array):
            name = runtime.item_type
        return child, declared, name, _render_type(child, name)
    if isinstance(runtime, Struct | Mapping):
        mapping = cast(Mapping[str, object], runtime)
        child = mapping.get(token, data)
        return child, Any, "Json", _render_type(child, "Json")
    if is_dataclass(runtime) and not isinstance(runtime, type):
        hints = _type_hints(type(runtime))
        raw = getattr(type(runtime), "__annotations__", {}).get(token)
        if token not in hints or not hasattr(runtime, token):
            return data, Any, "Json", "Json"
        child = getattr(runtime, token)
        declared = hints[token]
        name = _declared_name(raw, declared)
        return child, declared, name, _render_type(child, name)
    if isinstance(runtime, BaseModel):
        model_field = type(runtime).model_fields.get(token)
        if model_field is None or not hasattr(runtime, token):
            return data, Any, "Json", "Json"
        child = getattr(runtime, token)
        declared = model_field.annotation or Any
        name = _declared_name(declared, declared)
        return child, declared, name, _render_type(child, name)
    return data, Any, "Json", _render_type(data, "Json")


def _select_local_child(
    local: Local,
    token: str,
    data: object,
    *,
    source: str,
) -> tuple[object, object, str, str]:
    if token == "type":
        return local.type, str, "str", "Text"
    if token == "value":
        return local.value, Any, "Value | TypedPointer", local.type
    if token == "name":
        return local.name, str | None, "str | None", "Text"
    if token == "dim":
        return local.dim, Literal[0, 1], "Literal[0, 1]", "Number"
    raise ValueError(f"field does not exist ({token!r}): {source}")


def _type_hints(value: type[object]) -> dict[str, object]:
    try:
        return get_type_hints(value, include_extras=True)
    except (NameError, TypeError):
        return dict(getattr(value, "__annotations__", {}))


def _sequence_item_annotation(annotation: object) -> object:
    annotation = _runtime_branch(annotation)
    args = get_args(annotation)
    if not args:
        return Any
    return args[0]


def _runtime_branch(annotation: object) -> object:
    origin = get_origin(annotation)
    if origin is Annotated:
        args = get_args(annotation)
        return args[0] if args else annotation
    return annotation


def _declared_name(raw: object, annotation: object) -> str:
    if isinstance(raw, str):
        text = raw.strip().replace("typing.", "")
        match = _ARRAY_ANNOTATION_RE.fullmatch(text)
        if match:
            return f"{match.group(1)}[]"
        return text
    alias_name = getattr(annotation, "__name__", None)
    if isinstance(alias_name, str):
        return alias_name
    origin = get_origin(annotation)
    args = get_args(annotation)
    if origin in {tuple, list, Sequence} and args:
        return f"{_declared_name(args[0], args[0])}[]"
    if origin in {UnionType, Union} or isinstance(annotation, UnionType):
        return " | ".join(_declared_name(item, item) for item in args)
    if origin is Literal:
        return "Literal[" + ", ".join(repr(item) for item in args) + "]"
    if annotation is Any:
        return "Any"
    return str(annotation).replace("typing.", "")


def _render_type(value: object, declared: str) -> str:
    if isinstance(value, TypedPointer):
        return value.type
    if isinstance(value, Local):
        return value.type
    if isinstance(value, Array | Struct):
        return value.type
    if isinstance(value, str) and "ExecutionError" in declared:
        return "ExecutionError"
    if value is None:
        return "null"
    return declared


def _canonical_array_index(value: str) -> bool:
    return (
        bool(value)
        and value.isascii()
        and value.isdigit()
        and (value == "0" or not value.startswith("0"))
    )


@dataclass(frozen=True, slots=True)
class ThreadControlRefData:
    """One caller-facing thread-control reference."""

    thread: str
    index: int

    @classmethod
    def from_ref(cls, ref: ControlRef) -> ThreadControlRefData:
        return cls(thread=ref.target, index=ref.index)


@dataclass(frozen=True, slots=True)
class RunControlRefData:
    """One caller-facing run-control reference."""

    run: str
    index: int

    @classmethod
    def from_ref(cls, ref: ControlRef) -> RunControlRefData:
        return cls(run=ref.target, index=ref.index)


EjectionRefData = ThreadControlRefData | RunControlRefData


StepInputData = Pointer


@dataclass(frozen=True, slots=True)
class RunRequest:
    """One unresolved caller request for a new root run."""

    thread: str
    commands: tuple[RunOverride, ...]
    input: RunnableInputRaw
    session_commands: tuple[RunOverride, ...]
    runnable_fallbacks: tuple[str, ...]
    request_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.thread, str):
            raise TypeError("run request thread must be a string")
        if not self.thread or self.thread != self.thread.strip():
            raise ValueError("run request requires a canonical thread")
        if not isinstance(self.commands, tuple) or not all(
            isinstance(command, RunOverride) for command in self.commands
        ):
            raise TypeError("run request commands must be RunOverride objects")
        if not isinstance(self.input, RunnableInputRaw):
            raise TypeError("run request input must be RunnableInputRaw")
        if not isinstance(self.session_commands, tuple) or not all(
            isinstance(command, RunOverride) for command in self.session_commands
        ):
            raise TypeError("run request session commands must be RunOverride objects")
        if not isinstance(self.runnable_fallbacks, tuple) or not all(
            isinstance(candidate, str) for candidate in self.runnable_fallbacks
        ):
            raise TypeError("run request runnable fallbacks must be strings")
        if not self.runnable_fallbacks:
            raise ValueError("run request requires at least one runnable fallback")
        if any(
            not candidate or candidate != candidate.strip()
            for candidate in self.runnable_fallbacks
        ):
            raise ValueError("run request runnable fallbacks must be canonical strings")
        if len(self.runnable_fallbacks) != len(set(self.runnable_fallbacks)):
            raise ValueError("run request runnable fallbacks must be unique")
        if not isinstance(self.request_id, str):
            raise TypeError("run request ID must be a string")
        if not self.request_id or self.request_id != self.request_id.strip():
            raise ValueError("run request requires a canonical request ID")


@dataclass(frozen=True, slots=True)
class RetryRequest:
    """One unresolved caller request to reopen a root run."""

    source: str
    commands: tuple[RunOverride, ...]
    request_id: str
    anchor: StepPath | None = None

    def __post_init__(self) -> None:
        _validate_restart_request(
            source=self.source,
            commands=self.commands,
            request_id=self.request_id,
        )
        if self.anchor is not None and not isinstance(self.anchor, StepPath):
            raise TypeError("retry request anchor must be a StepPath or none")
        if self.anchor is not None and self.anchor.run != self.source:
            raise ValueError("retry request anchor must belong to its source run")


@dataclass(frozen=True, slots=True)
class RerunRequest:
    """One unresolved caller request to start a new root from a source run."""

    source: str
    commands: tuple[RunOverride, ...]
    request_id: str

    def __post_init__(self) -> None:
        _validate_restart_request(
            source=self.source,
            commands=self.commands,
            request_id=self.request_id,
        )


def _validate_restart_request(
    *,
    source: str,
    commands: tuple[RunOverride, ...],
    request_id: str,
) -> None:
    validate_execution_id(source, label="restart source run")
    if not isinstance(commands, tuple) or not all(
        isinstance(command, RunOverride) for command in commands
    ):
        raise TypeError("restart request commands must be RunOverride objects")
    if any(
        command.group == "default" and command.field == "runnable"
        for command in commands
    ):
        raise ValueError("restart request cannot replace the persisted runnable")
    if not isinstance(request_id, str):
        raise TypeError("restart request ID must be a string")
    if not request_id or request_id != request_id.strip():
        raise ValueError("restart request requires a canonical request ID")


@dataclass(frozen=True, slots=True)
class ThreadPeerInfo:
    """One caller-facing thread peer."""

    type: ThreadPeerType = "user"
    name: str = "user"
    thread: str | None = None

    @classmethod
    def from_peer(cls, peer: ThreadPeer) -> ThreadPeerInfo:
        return cls(type=peer.type, name=peer.name, thread=peer.thread)


@dataclass(frozen=True, slots=True)
class ThreadRunInfo:
    """One compact run summary embedded in a thread summary."""

    id: str
    status: RunStatus
    created_at: str
    started_at: str
    finished_at: str | None
    updated_at: str

    @classmethod
    def from_record(cls, run: RunRecord) -> ThreadRunInfo:
        return cls(
            id=run.id,
            status=run.status,
            created_at=run.created_at,
            started_at=run.started_at,
            finished_at=run.finished_at,
            updated_at=run.finished_at or run.started_at,
        )


@dataclass(frozen=True, slots=True)
class ThreadInfo:
    """One thread summary schema."""

    id: str
    title: str
    created_at: str
    updated_at: str
    origin: str
    channel: str
    status: str
    peer: ThreadPeerInfo
    created_by: ThreadControlRefData
    head: ThreadControlRefData
    run_count: int
    latest_run: ThreadRunInfo | None
    active_run: ThreadRunInfo | None

    @classmethod
    def from_records(
        cls,
        thread: ThreadRecord,
        runs: Sequence[RunRecord] = (),
        *,
        input_parts: Sequence[Part],
    ) -> ThreadInfo:
        """Build one thread summary from durable records."""

        if not runs:
            title = thread.peer.name if thread.peer.type == "agent" else thread.origin
            return cls(
                id=thread.thread_id,
                title=title,
                created_at=thread.created_at,
                origin=thread.origin,
                channel=_thread_channel(thread.thread_id, thread.origin),
                status="idle",
                updated_at=thread.updated_at,
                peer=ThreadPeerInfo.from_peer(thread.peer),
                created_by=ThreadControlRefData.from_ref(thread.created_by),
                head=ThreadControlRefData.from_ref(thread.head),
                run_count=0,
                latest_run=None,
                active_run=None,
            )
        last = runs[-1]
        active = next((run for run in reversed(runs) if run.status == "running"), None)
        return cls(
            id=thread.thread_id,
            title=message_summary(input_parts) or thread.origin,
            created_at=thread.created_at,
            origin=thread.origin,
            channel=_thread_channel(thread.thread_id, thread.origin),
            status="running" if active is not None else "idle",
            updated_at=max(last.finished_at or last.started_at, thread.updated_at),
            peer=ThreadPeerInfo.from_peer(thread.peer),
            created_by=ThreadControlRefData.from_ref(thread.created_by),
            head=ThreadControlRefData.from_ref(thread.head),
            run_count=len(runs),
            latest_run=ThreadRunInfo.from_record(last),
            active_run=(
                ThreadRunInfo.from_record(active) if active is not None else None
            ),
        )


@dataclass(frozen=True, slots=True)
class RunInfo:
    """One caller-facing run summary and identity schema."""

    id: str
    parent: StepPath | None
    thread_id: str
    root_run_id: str
    runnable_kind: str
    runnable_name: str | None
    call_kind: str
    state: RunControlRefData
    occurrence: Occurrence | None
    input_text: str
    summary: str
    status: RunStatus
    error: ExecutionError | None
    ejected: EjectionRefData | None
    created_at: str
    started_at: str
    finished_at: str | None
    updated_at: str

    @classmethod
    def from_record(
        cls,
        run: RunRecord,
        *,
        controls: Sequence[ControlRecord],
        steps: Sequence[StepRecord],
        root_run_id: str,
        error_message: str | None,
        ejection_scope: Literal["run", "thread"] | None,
        input_parts: Sequence[Part],
    ) -> RunInfo:
        """Build one run summary from durable records."""

        preparation = _preparation_payload(run, controls)
        input_text = message_summary(input_parts)
        kind, separator, name = preparation.runnable.partition(":")
        last_message_step = next(
            (
                step
                for step in reversed(steps)
                if step.output and step_message_role(step.kind) is not None
            ),
            None,
        )
        summary = (
            message_summary(_local_parts(last_message_step.output))
            if last_message_step is not None
            else input_text
        )
        if (
            run.status == "failed"
            and error_message
            and (not summary or summary == input_text)
        ):
            summary = error_message
        return cls(
            id=run.id,
            parent=run.parent,
            thread_id=run.thread,
            root_run_id=root_run_id,
            runnable_kind=kind if separator else "",
            runnable_name=name if separator else preparation.runnable,
            call_kind="top" if run.parent is None else "run",
            state=RunControlRefData.from_ref(run.state),
            occurrence=run.occurrence,
            input_text=input_text,
            summary=summary,
            status=run.status,
            error=run.error,
            ejected=_ejection_ref_data(run.ejected_by, scope=ejection_scope),
            created_at=run.created_at,
            started_at=run.started_at,
            finished_at=run.finished_at,
            updated_at=run.finished_at or run.started_at,
        )


@dataclass(frozen=True, slots=True)
class ControlInfo:
    """One accepted execution control."""

    run_id: str
    index: int
    kind: ControlKind
    timing: ControlTiming
    request_id: str | None
    status: ControlStatus
    payload: ControlPayloadField
    error: str | None
    created_at: str
    finished_at: str | None

    @classmethod
    def from_record(cls, run: RunRecord, control: ControlRecord) -> ControlInfo:
        return cls(
            run_id=run.id,
            index=control.index,
            kind=control.kind,
            timing=control.timing,
            request_id=control.request,
            status=control.status,
            payload=control.payload,
            error=control.error,
            created_at=control.created_at,
            finished_at=control.finished_at,
        )


@dataclass(frozen=True, slots=True)
class StepData:
    """One caller-facing execution step."""

    path: StepPath
    kind: StepKind
    input: list[StepInputData]
    given: StepGiven
    state: RunControlRefData
    output: Local | None
    occurrence: Occurrence | None = None
    noted: StepNoted = None
    status: StepStatus = "running"
    error: ExecutionError | None = None
    ejected_by: RunControlRefData | None = None
    created_at: str = ""
    started_at: str = ""
    finished_at: str | None = None

    def __post_init__(self) -> None:
        validate_occurrence(self.occurrence)
        validate_step_given(self.kind, self.given)
        validate_step_noted(self.kind, self.noted, self.status)

    @classmethod
    def from_record(
        cls,
        step: StepRecord,
        *,
        call: ModelCall | None = None,
    ) -> StepData:
        if isinstance(step.given, StoredModelStepGiven):
            if call is None:
                raise ValueError(f"model call is missing for Step {step.path}")
            given: StepGiven = ModelStepGiven(model=step.given.model, call=call)
        else:
            given = step.given
        return cls(
            path=step.path,
            kind=step.kind,
            input=list(step.input),
            output=step.output,
            occurrence=step.occurrence,
            given=given,
            state=RunControlRefData.from_ref(step.state),
            noted=step.noted,
            status=step.status,
            error=step.error,
            ejected_by=(
                RunControlRefData.from_ref(step.ejected_by)
                if step.ejected_by is not None
                else None
            ),
            created_at=step.created_at,
            started_at=step.started_at,
            finished_at=step.finished_at,
        )


@dataclass(frozen=True, slots=True)
class RunDetail(RunInfo):
    """One complete run detail schema."""

    control: RunControlRefData
    output: Local | None
    controls: list[ControlInfo]
    steps: list[StepData] = field(default_factory=list)

    @classmethod
    def from_record(
        cls,
        run: RunRecord,
        *,
        steps: Sequence[StepRecord],
        controls: Sequence[ControlRecord] = (),
        model_calls: Mapping[StepPath, ModelCall] | None = None,
        root_run_id: str,
        error_message: str | None,
        ejection_scope: Literal["run", "thread"] | None,
        input_parts: Sequence[Part],
    ) -> RunDetail:
        """Build complete caller-facing run detail from durable records."""

        info = RunInfo.from_record(
            run,
            controls=controls,
            steps=steps,
            root_run_id=root_run_id,
            error_message=error_message,
            ejection_scope=ejection_scope,
            input_parts=input_parts,
        )
        return cls(
            **{item.name: getattr(info, item.name) for item in fields(RunInfo)},
            control=RunControlRefData.from_ref(run.control),
            output=run.output,
            controls=[ControlInfo.from_record(run, item) for item in controls],
            steps=[
                StepData.from_record(
                    step,
                    call=(model_calls or {}).get(step.path),
                )
                for step in steps
            ],
        )


@dataclass(frozen=True, slots=True)
class ThreadDetail(ThreadInfo):
    """One complete thread detail schema."""

    runs: list[RunDetail] = field(default_factory=list)

    @classmethod
    def from_info(cls, info: ThreadInfo, *, runs: Sequence[RunDetail]) -> ThreadDetail:
        return cls(
            **{item.name: getattr(info, item.name) for item in fields(ThreadInfo)},
            runs=list(runs),
        )


def _thread_channel(thread_id: str, origin: str) -> str:
    if origin != "chat":
        return ""
    if thread_id.startswith("web_"):
        return "web"
    if thread_id.startswith("script_tg_"):
        return "tg"
    return "terminal"


def _preparation_payload(
    run: RunRecord,
    controls: Sequence[ControlRecord],
) -> PreparationControlPayload:
    for control in controls:
        if control.index == run.control.index and isinstance(
            control.payload, PreparationControlPayload
        ):
            return control.payload
    raise ValueError(f"run preparation control not found: {run.id}@{run.control.index}")


def _local_parts(local: Local | None) -> tuple[Part, ...]:
    if local is None:
        return ()
    return parts_from_local(local)


def _ejection_ref_data(
    ref: ControlRef | None,
    *,
    scope: Literal["run", "thread"] | None,
) -> EjectionRefData | None:
    if ref is None:
        return None
    if scope == "thread":
        return ThreadControlRefData.from_ref(ref)
    if scope == "run":
        return RunControlRefData.from_ref(ref)
    raise ValueError(f"ejection scope is required: {ref.target}@{ref.index}")
