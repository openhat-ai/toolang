"""Execution-side detail view types."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from toolang.base.types.message import message_summary
from .events import MessageData, run_input_record_message_data, run_input_message_data, step_message_data
from .records import InputRecord, RunRecord, RunStatus, StepRecord, ThreadPeer, ThreadRecord


@dataclass(frozen=True, slots=True)
class ThreadRunInfo:
    """One compact run summary embedded in a thread summary."""

    id: str
    origin: str
    status: RunStatus
    created_at: str
    started_at: str
    finished_at: str | None
    updated_at: str


@dataclass(frozen=True, slots=True)
class ThreadInfo:
    """One thread summary payload."""

    id: str
    title: str
    created_at: str
    updated_at: str
    origin: str
    channel: str
    status: str
    peer: ThreadPeer
    parent: str | None
    run_count: int
    latest_run: ThreadRunInfo | None
    active_run: ThreadRunInfo | None


@dataclass(frozen=True, slots=True)
class RunInfo:
    """One run identity payload."""

    id: str
    origin: str
    thread_id: str
    root_run_id: str
    parent_run_id: str | None
    parent_step_index: int | None
    executable_kind: str
    executable_name: str | None
    call_kind: str
    metadata: dict[str, object]
    superseded: dict[str, object] | None
    created_at: str
    started_at: str
    finished_at: str | None
    updated_at: str


@dataclass(frozen=True, slots=True)
class StepDetail:
    """One step detail payload."""

    record: StepRecord
    message: MessageData | None


@dataclass(frozen=True, slots=True)
class InputDetail:
    """One run input detail payload."""

    record: InputRecord
    message: MessageData | None


@dataclass(frozen=True, slots=True)
class RunOutput:
    """One run output payload."""

    status: RunStatus
    error: str | None
    steps: list[StepDetail] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class RunDetail:
    """One run detail payload."""

    info: RunInfo
    input: MessageData | None
    inputs: list[InputDetail]
    output: RunOutput


@dataclass(frozen=True, slots=True)
class ThreadDetail:
    """One thread detail payload."""

    info: ThreadInfo
    runs: list[RunDetail] = field(default_factory=list)


def thread_info_from_runs(
    thread_id: str,
    runs: Sequence[RunRecord],
    *,
    inputs_by_run: Mapping[str, Sequence[InputRecord]],
    steps_by_run: Mapping[str, Sequence[StepRecord]],
    thread: ThreadRecord | None = None,
) -> ThreadInfo:
    """Build one thread summary from ordered run records."""

    first = runs[0]
    last = runs[-1]
    active = next((run for run in reversed(runs) if run.status == "running"), None)
    first_input = run_input_message_data(first, _start_input(inputs_by_run.get(first.run_id, ())))
    title = message_summary(first_input.parts) or first.origin
    updated_at = last.finished_at or last.started_at
    if thread is not None:
        updated_at = max(updated_at, thread.updated_at)
    return ThreadInfo(
        id=thread_id,
        title=title,
        created_at=thread.created_at if thread is not None else first.created_at,
        origin=last.origin,
        channel=_thread_channel(thread_id, last.origin),
        status=_thread_status(active),
        updated_at=updated_at,
        peer=thread.peer if thread is not None else ThreadPeer(),
        parent=thread.parent if thread is not None else None,
        run_count=len(runs),
        latest_run=thread_run_info_from_record(last),
        active_run=thread_run_info_from_record(active) if active is not None else None,
    )


def thread_info_from_record(thread: ThreadRecord) -> ThreadInfo:
    """Build one thread summary from metadata when no runs exist."""

    title = thread.peer.name if thread.peer.type == "agent" else thread.origin
    return ThreadInfo(
        id=thread.thread_id,
        title=title,
        created_at=thread.created_at,
        origin=thread.origin,
        channel=_thread_channel(thread.thread_id, thread.origin),
        status="idle",
        updated_at=thread.updated_at,
        peer=thread.peer,
        parent=thread.parent,
        run_count=0,
        latest_run=None,
        active_run=None,
    )


def thread_run_info_from_record(run: RunRecord) -> ThreadRunInfo:
    """Build one compact run summary for thread list payloads."""

    return ThreadRunInfo(
        id=run.run_id,
        origin=run.origin,
        status=run.status,
        created_at=run.created_at,
        started_at=run.started_at,
        finished_at=run.finished_at,
        updated_at=run.finished_at or run.started_at,
    )


def run_info_from_record(run: RunRecord) -> RunInfo:
    """Build one run info payload from one durable run record."""

    return RunInfo(
        id=run.run_id,
        origin=run.origin,
        thread_id=run.thread_id,
        root_run_id=run.root_run_id,
        parent_run_id=run.parent_run_id,
        parent_step_index=run.parent_step_index,
        executable_kind=run.executable_kind,
        executable_name=run.executable_name,
        call_kind=run.call_kind,
        metadata=dict(run.metadata),
        superseded=run.superseded,
        created_at=run.created_at,
        started_at=run.started_at,
        finished_at=run.finished_at,
        updated_at=run.finished_at or run.started_at,
    )


def _thread_channel(thread_id: str, origin: str) -> str:
    if origin != "chat":
        return ""
    if thread_id.startswith("web_"):
        return "web"
    if thread_id.startswith("script_tg_"):
        return "tg"
    return "terminal"


def _thread_status(active: RunRecord | None) -> str:
    return "running" if active is not None else "idle"


def run_input_from_records(run: RunRecord, *, inputs: Sequence[InputRecord]) -> MessageData | None:
    """Build the start input payload from durable input records."""

    start = _start_input(inputs)
    return run_input_message_data(run, start)


def run_inputs_from_records(run: RunRecord, *, inputs: Sequence[InputRecord]) -> list[InputDetail]:
    """Build run input details from durable input records."""

    return [
        InputDetail(record=input, message=run_input_record_message_data(run, input))
        for input in inputs
    ]


def run_output_from_steps(
    run: RunRecord,
    *,
    steps: Sequence[StepRecord],
) -> RunOutput:
    """Build one run output payload from one durable run record and step trace."""

    step_details = [
        StepDetail(record=step, message=step_message_data(run, step))
        for step in steps
    ]
    return RunOutput(
        status=run.status,
        error=run.error,
        steps=step_details,
    )


def run_detail_from_record(
    run: RunRecord,
    *,
    steps: Sequence[StepRecord],
    inputs: Sequence[InputRecord] = (),
) -> RunDetail:
    """Build one run detail payload from one durable run record."""

    return RunDetail(
        info=run_info_from_record(run),
        input=run_input_from_records(run, inputs=inputs),
        inputs=run_inputs_from_records(run, inputs=inputs),
        output=run_output_from_steps(run, steps=steps),
    )


def _start_input(inputs: Sequence[InputRecord]) -> InputRecord:
    for input in inputs:
        if input.index == 0 and input.action == "start":
            return input
    raise ValueError("run start input not found")
