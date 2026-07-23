"""Caller-facing execution protocol schemas."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, fields
from typing import Any

from toolang.base.types.message import Message, Part, message_summary
from .records import (
    OutputRef,
    RunControlRecord,
    RunControlRef,
    RunRecord,
    StepRecord,
    ThreadControlRef,
    ThreadPeer,
    ThreadRecord,
    step_message_role,
)
from .types import (
    ControlTiming,
    RunControlKind,
    ControlStatus,
    RunStatus,
    StepKind,
    StepPath,
    StepStatus,
    ThreadPeerType,
)


@dataclass(frozen=True, slots=True)
class RunControlRefData:
    """One caller-facing run-control input reference."""

    index: int = 0
    part: int | None = None

    @classmethod
    def from_ref(cls, ref: RunControlRef) -> RunControlRefData:
        return cls(index=ref.index, part=ref.part)


@dataclass(frozen=True, slots=True)
class ThreadControlRefData:
    """One caller-facing thread-control reference."""

    thread: str
    index: int

    @classmethod
    def from_ref(cls, ref: ThreadControlRef) -> ThreadControlRefData:
        return cls(thread=ref.thread, index=ref.index)


@dataclass(frozen=True, slots=True)
class OutputRefData:
    """One caller-facing step-output reference."""

    step: StepPath
    part: int | None = None

    @classmethod
    def from_ref(cls, ref: OutputRef) -> OutputRefData:
        return cls(step=ref.step, part=ref.part)


StepInputData = RunControlRefData | OutputRefData | Message


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
class FailureDetail:
    """One normalized run failure schema."""

    reason: str
    step_index: int | None = None
    step_kind: StepKind | None = None
    step_error: str | None = None

    @classmethod
    def from_run(
        cls,
        *,
        status: str,
        error: str | None,
        steps: Sequence[StepRecord],
    ) -> FailureDetail | None:
        if status != "failed" and error is None:
            return None
        failed_step = next(
            (item for item in reversed(steps) if item.status == "failed"), None
        )
        step_error = failed_step.error if failed_step is not None else None
        return cls(
            reason=error or step_error or "Run failed.",
            step_index=failed_step.index if failed_step is not None else None,
            step_kind=failed_step.kind if failed_step is not None else None,
            step_error=step_error,
        )


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
            title=message_summary(start.input.parts) or thread.origin,
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
    parent: str | None
    thread_id: str
    root_run_id: str
    runnable_kind: str
    runnable_name: str | None
    call_kind: str
    metadata: dict[str, object]
    input_text: str
    summary: str
    status: RunStatus
    error: str | None
    superseded_by: ThreadControlRefData | None
    failure: FailureDetail | None
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
    ) -> RunInfo:
        """Build one run summary from durable records."""

        start = _start_control(controls)
        input_text = (
            message_summary(start.input.parts) if start.input is not None else ""
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
        if (
            run.status == "failed"
            and run.error
            and (not summary or summary == input_text)
        ):
            summary = run.error
        return cls(
            id=run.id,
            parent=run.parent,
            thread_id=run.thread,
            root_run_id=run.root_run_id,
            runnable_kind=run.runnable_kind,
            runnable_name=run.runnable_name,
            call_kind=run.call_kind,
            metadata=dict(run.context),
            input_text=input_text,
            summary=summary,
            status=run.status,
            error=run.error,
            superseded_by=(
                ThreadControlRefData.from_ref(run.superseded_by)
                if run.superseded_by is not None
                else None
            ),
            failure=FailureDetail.from_run(
                status=run.status,
                error=run.error,
                steps=steps,
            ),
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
    message: Message | None
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
            request_id=control.request_id,
            status=control.status,
            message=control.input,
            context=dict(control.context),
            error=control.error,
            created_at=control.created_at,
            finished_at=control.finished_at,
        )


@dataclass(frozen=True, slots=True)
class ThreadResult:
    """One thread mutation result."""

    thread: ThreadInfo


@dataclass(frozen=True, slots=True)
class ChatResult:
    """One completed chat exchange."""

    thread: ThreadInfo
    run: RunInfo
    message: Message
    assistant: Message


@dataclass(frozen=True, slots=True)
class StepData:
    """One caller-facing execution step."""

    parent: StepPath
    index: int
    kind: StepKind
    input: list[StepInputData]
    output: list[Part]
    context: dict[str, Any] = field(default_factory=dict)
    detail: dict[str, Any] = field(default_factory=dict)
    status: StepStatus = "running"
    error: str | None = None
    created_at: str = ""
    started_at: str = ""
    finished_at: str | None = None

    @classmethod
    def from_record(cls, step: StepRecord) -> StepData:
        return cls(
            parent=step.parent,
            index=step.index,
            kind=step.kind,
            input=[_step_input_data(item) for item in step.input],
            output=list(step.output),
            context=dict(step.context),
            detail=dict(step.detail),
            status=step.status,
            error=step.error,
            created_at=step.created_at,
            started_at=step.started_at,
            finished_at=step.finished_at,
        )


@dataclass(frozen=True, slots=True)
class RunOutput:
    """One run output schema."""

    status: RunStatus
    error: str | None
    failure: FailureDetail | None
    steps: list[StepData] = field(default_factory=list)

    @classmethod
    def from_record(cls, run: RunRecord, *, steps: Sequence[StepRecord]) -> RunOutput:
        return cls(
            status=run.status,
            error=run.error,
            failure=FailureDetail.from_run(
                status=run.status,
                error=run.error,
                steps=steps,
            ),
            steps=[StepData.from_record(step) for step in steps],
        )


@dataclass(frozen=True, slots=True)
class RunDetail(RunInfo):
    """One complete run detail schema."""

    input: Message | None
    inputs: list[RunControlInfo]
    output: RunOutput
    prompts: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_record(
        cls,
        run: RunRecord,
        *,
        steps: Sequence[StepRecord],
        controls: Sequence[RunControlRecord] = (),
        prompts: Mapping[str, str] | None = None,
    ) -> RunDetail:
        """Build complete caller-facing run detail from durable records."""

        info = RunInfo.from_record(run, controls=controls, steps=steps)
        start = _start_control(controls)
        return cls(
            **{item.name: getattr(info, item.name) for item in fields(RunInfo)},
            input=start.input,
            inputs=[RunControlInfo.from_record(run, item) for item in controls],
            output=RunOutput.from_record(run, steps=steps),
            prompts=dict(prompts or {}),
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


def _step_input_data(item: RunControlRef | OutputRef | Message) -> StepInputData:
    if isinstance(item, RunControlRef):
        return RunControlRefData.from_ref(item)
    if isinstance(item, OutputRef):
        return OutputRefData.from_ref(item)
    return item


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
        if control.index == 0 and control.kind == "start":
            return control
    raise ValueError("run start input not found")
