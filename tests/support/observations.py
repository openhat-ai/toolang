"""Build compact observation fixtures independently of durable record fixtures."""

from __future__ import annotations

from toolang.execution.events import ModelStepInfo, PartEnd
from toolang.execution.event_projection import part_data
from toolang.base.types.message import Part

from toolang.execution.types import StepRef

from toolang.execution.events import (
    RunBegin,
    RunEnd,
    StepBegin,
    StepEnd,
    EventOutput,
    StepInfo,
    StepSummary,
    ControlApplied,
    FlowStepInfo,
    ToolStepInfo,
)
from toolang.execution.types import (
    ControlRef,
    ErrorMessage,
    ErrorRef,
    Occurrence,
    Output,
    StepSpec,
    StepKind,
    StepReport,
    StepStatus,
    RunStatus,
)
from toolang.execution.event_projection import step_info, step_summary, direct_output


def run_begin(
    run: str,
    control: ControlRef | int,
    runnable: str = "",
    parent: StepRef | str | None = None,
    occurrence: Occurrence | None = None,
    started_at: str = "",
    thread_id: str = "",
) -> RunBegin:
    return RunBegin(
        run,
        control.index if isinstance(control, ControlRef) else control,
        runnable,
        str(parent) if parent is not None else None,
        occurrence,
        started_at,
        thread_id,
    )


def step_begin(
    step: StepRef | str,
    kind: StepKind,
    spec: StepSpec | StepInfo,
    state: ControlRef | None = None,
    input: object = (),
    preceded_by: tuple[ControlRef | ControlApplied, ...] = (),
    occurrence: Occurrence | None = None,
    started_at: str = "",
) -> StepBegin:
    del state, input
    ref = StepRef.parse(step)
    return StepBegin(
        str(ref),
        ref.run_id,
        str(ref.parent) if ref.parent else None,
        ref.index,
        kind,
        spec
        if isinstance(spec, FlowStepInfo | ModelStepInfo | ToolStepInfo)
        else step_info(spec),
        tuple(
            ControlApplied(str(item.target), item.index)
            if isinstance(item, ControlRef)
            else item
            for item in preceded_by
        ),
        occurrence,
        started_at,
    )


def step_end(
    step: StepRef | str,
    kind: StepKind,
    status: StepStatus,
    output: Output | EventOutput | None = None,
    report: StepReport | StepSummary = None,
    error: str | ErrorMessage | ErrorRef | None = None,
    aborted_by: ControlRef | None = None,
    finished_at: str = "",
) -> StepEnd:
    del aborted_by
    ref = StepRef.parse(step)
    return StepEnd(
        step=str(ref),
        run=ref.run_id,
        kind=kind,
        status=status,
        output=output if isinstance(output, EventOutput) else direct_output(output),
        report=report if isinstance(report, StepSummary) else step_summary(report),
        error=error.message
        if isinstance(error, ErrorMessage)
        else error
        if isinstance(error, str)
        else None,
        error_source=str(error.ref.record) if isinstance(error, ErrorRef) else None,
        finished_at=finished_at,
    )


def run_end(
    run: str,
    status: RunStatus,
    control: ControlRef | int | None = None,
    output: Output | EventOutput | None = None,
    error: str | ErrorMessage | ErrorRef | None = None,
    finished_at: str = "",
) -> RunEnd:
    return RunEnd(
        run=run,
        status=status,
        control=ControlApplied(str(control.target), control.index)
        if isinstance(control, ControlRef)
        else ControlApplied(run, control)
        if control is not None
        else None,
        output=output if isinstance(output, EventOutput) else direct_output(output),
        error=error.message
        if isinstance(error, ErrorMessage)
        else error
        if isinstance(error, str)
        else None,
        error_source=str(error.ref.record) if isinstance(error, ErrorRef) else None,
        finished_at=finished_at,
    )


def part_end(step: str, part: int, data: Part) -> PartEnd:
    return PartEnd(step, part, part_data(data))
