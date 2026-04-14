"""Execution-side detail view types."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from toolang.base.types.message import message_text
from .events import MessageData, run_input_message_data, step_message_data
from .records import RunRecord, RunStatus, StepRecord


@dataclass(frozen=True, slots=True)
class ThreadInfo:
    """One thread summary payload."""

    id: str
    title: str
    updated_at: str
    origin: str


@dataclass(frozen=True, slots=True)
class RunInfo:
    """One run identity payload."""

    id: str
    origin: str
    thread_id: str
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
    steps_by_run: Mapping[str, Sequence[StepRecord]],
) -> ThreadInfo:
    """Build one thread summary from ordered run records."""

    first = runs[0]
    last = runs[-1]
    first_input = run_input_message_data(first)
    title = message_text(first_input.parts) or first.origin
    return ThreadInfo(
        id=thread_id,
        title=title,
        origin=last.origin,
        updated_at=last.finished_at or last.started_at,
    )


def run_info_from_record(run: RunRecord) -> RunInfo:
    """Build one run info payload from one durable run record."""

    return RunInfo(
        id=run.run_id,
        origin=run.origin,
        thread_id=run.thread_id,
        created_at=run.created_at,
        started_at=run.started_at,
        finished_at=run.finished_at,
        updated_at=run.finished_at or run.started_at,
    )


def run_input_from_steps(run: RunRecord, *, steps: Sequence[StepRecord]) -> MessageData | None:
    """Build one run input payload from one durable run record and step trace."""

    del steps
    return run_input_message_data(run)


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
) -> RunDetail:
    """Build one run detail payload from one durable run record."""

    return RunDetail(
        info=run_info_from_record(run),
        input=run_input_from_steps(run, steps=steps),
        output=run_output_from_steps(run, steps=steps),
    )
