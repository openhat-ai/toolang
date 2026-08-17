"""Caller-facing execution protocol schemas."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, fields
from typing import Any

from toolang.base.types.message import MessagePart, message_summary
from toolang.base.types.run import ModelCall
from .records import (
    ControlPayload,
    PreparationControlPayload,
    RunControlRecord,
    RunRecord,
    StepRecord,
    ThreadPeer,
    ThreadRecord,
    model_call_to_data,
    execution_error_message,
    step_message_role,
)
from .types import (
    ControlRef,
    ControlTiming,
    RunControlKind,
    ControlStatus,
    ExecutionError,
    Local,
    RunStatus,
    StepKind,
    StepPath,
    StepStatus,
    ThreadPeerType,
    ValuePtr,
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


StepInputData = ValuePtr


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
        payload = _preparation_payload(
            first,
            (controls_by_run or {}).get(first.id, ()),
        )
        primary = _primary_parts(payload)
        return cls(
            id=thread.thread_id,
            title=message_summary(primary) or thread.origin,
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

        preparation = _preparation_payload(run, controls)
        input_text = message_summary(_primary_parts(preparation))
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
            runnable_kind=kind if separator else "",
            runnable_name=name if separator else preparation.runnable,
            call_kind="top" if run.parent is None else "run",
            metadata=dict(run.placement or {}),
            input_text=input_text,
            summary=summary,
            status=run.status,
            error=run.error,
            ejected=_ejection_ref_data(run.ejected_by),
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
    request_id: str | None
    status: ControlStatus
    payload: ControlPayload
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
    output: Local | None
    placement: dict[str, object] | None = None
    given: dict[str, Any] = field(default_factory=dict)
    noted: dict[str, Any] = field(default_factory=dict)
    status: StepStatus = "running"
    error: ExecutionError | None = None
    ejected_by: RunControlRefData | None = None
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
            input=list(step.input),
            output=step.output,
            placement=step.placement,
            given=given,
            noted=dict(step.noted),
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
        return cls(
            **{item.name: getattr(info, item.name) for item in fields(RunInfo)},
            control=RunControlRefData.from_ref(run.control),
            output=run.output,
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
    controls: Sequence[RunControlRecord],
) -> PreparationControlPayload:
    for control in controls:
        if control.index == run.control.index and isinstance(
            control.payload, PreparationControlPayload
        ):
            return control.payload
    raise ValueError(f"run preparation control not found: {run.id}^{run.control.index}")


def _primary_parts(payload: PreparationControlPayload) -> tuple[MessagePart, ...]:
    if payload.locals is None:
        return ()
    primary = next((local for local in payload.locals if local.name == "_"), None)
    return _local_parts(primary)


def _local_parts(local: Local | None) -> tuple[MessagePart, ...]:
    if local is None:
        return ()
    value = local.value
    if isinstance(value, MessagePart):
        return (value,)
    if isinstance(value, tuple | list):
        return tuple(item for item in value if isinstance(item, MessagePart))
    return ()


def _ejection_ref_data(
    ref: ControlRef | None,
) -> EjectionRefData | None:
    if ref is None:
        return None
    if not ref.target.startswith("run_"):
        return ThreadControlRefData.from_ref(ref)
    return RunControlRefData.from_ref(ref)
