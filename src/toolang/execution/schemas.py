"""Caller-facing execution protocol schemas."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, fields
from typing import Any, cast

from toolang.base.types.message import Message, MessagePart, message_summary
from toolang.base.types.policy import RunBindings, RunLimits
from toolang.base.types.run import ModelCall
from toolang.lang.input import RunInput
from .records import (
    RunControlRecord,
    RunControlRef,
    RunInputRef,
    RunRecord,
    StepInput,
    StepOutputRef,
    StepRecord,
    ThreadControlRef,
    ThreadPeer,
    ThreadRecord,
    model_call_to_data,
    execution_error_message,
    step_message_role,
)
from .types import (
    ControlTiming,
    AgentResources,
    RunControlKind,
    ControlStatus,
    ExecutionError,
    RunStatus,
    StepKind,
    StepPath,
    StepStatus,
    ThreadPeerType,
)


@dataclass(frozen=True, slots=True)
class RunInputRefData:
    """One caller-facing run input reference."""

    index: int = 0
    name: str | None = None
    part: int | None = None

    @classmethod
    def from_ref(cls, ref: RunInputRef) -> RunInputRefData:
        return cls(index=ref.index, name=ref.name, part=ref.part)


@dataclass(frozen=True, slots=True)
class ThreadControlRefData:
    """One caller-facing thread-control reference."""

    thread: str
    index: int

    @classmethod
    def from_ref(cls, ref: ThreadControlRef) -> ThreadControlRefData:
        return cls(thread=ref.thread, index=ref.index)


@dataclass(frozen=True, slots=True)
class RunControlRefData:
    """One caller-facing run-control reference."""

    run: str
    index: int

    @classmethod
    def from_ref(cls, ref: RunControlRef) -> RunControlRefData:
        return cls(run=ref.run, index=ref.index)


EjectionRefData = ThreadControlRefData | RunControlRefData


@dataclass(frozen=True, slots=True)
class StepOutputRefData:
    """One caller-facing step-output reference."""

    step: StepPath
    part: int | None = None

    @classmethod
    def from_ref(cls, ref: StepOutputRef) -> StepOutputRefData:
        return cls(step=ref.step, part=ref.part)


StepInputData = RunInputRefData | StepOutputRefData | Message


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
        controls_by_run: Mapping[str, Sequence[RunControlRecord]] | None = None,
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
        first = runs[0]
        last = runs[-1]
        active = next((run for run in reversed(runs) if run.status == "running"), None)
        start = _start_control((controls_by_run or {}).get(first.id, ()))
        if start.input is None:
            raise ValueError(f"run start input has no message: {first.id}")
        return cls(
            id=thread.thread_id,
            title=message_summary(start.input.primary) or thread.origin,
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
    metadata: dict[str, object]
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
        controls: Sequence[RunControlRecord],
        steps: Sequence[StepRecord],
        root_run_id: str,
    ) -> RunInfo:
        """Build one run summary from durable records."""

        start = _start_control(controls)
        input_text = (
            message_summary(start.input.primary) if start.input is not None else ""
        )
        last_message_step = next(
            (
                step
                for step in reversed(steps)
                if step.output and step_message_role(step.kind) is not None
            ),
            None,
        )
        summary = (
            message_summary(last_message_step.output)
            if last_message_step is not None
            else input_text
        )
        error_message = execution_error_message(run.error, steps)
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
            runnable_kind=run.runnable_kind,
            runnable_name=run.runnable_name,
            call_kind=run.call_kind,
            metadata=cast(dict[str, object], dict(run.context)),
            input_text=input_text,
            summary=summary,
            status=run.status,
            error=run.error,
            ejected=_ejection_ref_data(run.ejected),
            created_at=run.created_at,
            started_at=run.started_at,
            finished_at=run.finished_at,
            updated_at=run.finished_at or run.started_at,
        )


@dataclass(frozen=True, slots=True)
class RunControlInfo:
    """One accepted control sent to a run."""

    run_id: str
    index: int
    kind: RunControlKind
    timing: ControlTiming
    source: str | None
    anchor: StepPath | None
    request_id: str | None
    status: ControlStatus
    input: RunInput | None
    bindings: RunBindings | None
    limits: RunLimits | None
    resources: AgentResources | None
    message: Message | None
    reason: str | None
    context: dict[str, Any]
    error: str | None
    created_at: str
    finished_at: str | None

    @classmethod
    def from_record(cls, run: RunRecord, control: RunControlRecord) -> RunControlInfo:
        return cls(
            run_id=run.id,
            index=control.index,
            kind=control.kind,
            timing=control.timing,
            source=control.source,
            anchor=control.anchor,
            request_id=control.request,
            status=control.status,
            input=control.input,
            bindings=control.bindings,
            limits=control.limits,
            resources=control.resources,
            message=control.message,
            reason=control.reason,
            context=dict(control.context),
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
    output: list[MessagePart]
    given: dict[str, Any] = field(default_factory=dict)
    noted: dict[str, Any] = field(default_factory=dict)
    status: StepStatus = "running"
    error: ExecutionError | None = None
    ejected: RunControlRefData | None = None
    created_at: str = ""
    started_at: str = ""
    finished_at: str | None = None

    @classmethod
    def from_record(
        cls,
        step: StepRecord,
        *,
        call: ModelCall | None = None,
    ) -> StepData:
        given = dict(step.given)
        if call is not None:
            given["call"] = model_call_to_data(call)
        return cls(
            path=step.path,
            kind=step.kind,
            input=[_step_input_data(item) for item in step.input],
            output=list(step.output),
            given=given,
            noted=dict(step.noted),
            status=step.status,
            error=step.error,
            ejected=(
                RunControlRefData.from_ref(step.ejected)
                if step.ejected is not None
                else None
            ),
            created_at=step.created_at,
            started_at=step.started_at,
            finished_at=step.finished_at,
        )


@dataclass(frozen=True, slots=True)
class RunDetail(RunInfo):
    """One complete run detail schema."""

    input: Message | None
    output: list[MessagePart] | None
    controls: list[RunControlInfo]
    steps: list[StepData] = field(default_factory=list)

    @classmethod
    def from_record(
        cls,
        run: RunRecord,
        *,
        steps: Sequence[StepRecord],
        controls: Sequence[RunControlRecord] = (),
        model_calls: Mapping[StepPath, ModelCall] | None = None,
        root_run_id: str,
    ) -> RunDetail:
        """Build complete caller-facing run detail from durable records."""

        info = RunInfo.from_record(
            run,
            controls=controls,
            steps=steps,
            root_run_id=root_run_id,
        )
        start = _start_control(controls)
        return cls(
            **{item.name: getattr(info, item.name) for item in fields(RunInfo)},
            input=(
                Message(role="user", parts=start.input.primary)
                if start.input is not None
                else None
            ),
            output=(
                list(_resolve_value_ref(run.output, controls=controls, steps=steps))
                if run.output is not None
                else None
            ),
            controls=[RunControlInfo.from_record(run, item) for item in controls],
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


def _step_input_data(item: StepInput) -> StepInputData:
    if isinstance(item, RunInputRef):
        return RunInputRefData.from_ref(item)
    if isinstance(item, StepOutputRef):
        return StepOutputRefData.from_ref(item)
    return item


def _resolve_value_ref(
    ref: RunInputRef | StepOutputRef,
    *,
    controls: Sequence[RunControlRecord],
    steps: Sequence[StepRecord],
) -> tuple[MessagePart, ...]:
    if isinstance(ref, StepOutputRef):
        return ref.resolve(steps)
    control = next((item for item in controls if item.index == ref.index), None)
    if control is None or control.input is None:
        return ()
    if ref.name is not None:
        value = next(
            (item.value for item in control.input.named if item.name == ref.name),
            None,
        )
        if isinstance(value, MessagePart):
            parts: tuple[MessagePart, ...] = (value,)
        elif isinstance(value, tuple) and all(
            isinstance(item, MessagePart) for item in value
        ):
            parts = cast(tuple[MessagePart, ...], value)
        else:
            return ()
    else:
        parts = control.input.primary
    if ref.part is None:
        return parts
    if 0 <= ref.part < len(parts):
        return (parts[ref.part],)
    return ()


def _thread_channel(thread_id: str, origin: str) -> str:
    if origin != "chat":
        return ""
    if thread_id.startswith("web_"):
        return "web"
    if thread_id.startswith("script_tg_"):
        return "tg"
    return "terminal"


def _start_control(controls: Sequence[RunControlRecord]) -> RunControlRecord:
    for control in controls:
        if control.index == 0 and control.kind in {"start", "rerun"}:
            return control
    raise ValueError("run start input not found")


def _ejection_ref_data(
    ref: ThreadControlRef | RunControlRef | None,
) -> EjectionRefData | None:
    if isinstance(ref, ThreadControlRef):
        return ThreadControlRefData.from_ref(ref)
    if isinstance(ref, RunControlRef):
        return RunControlRefData.from_ref(ref)
    return None
