"""Pure projection of in-memory execution facts into final client Event data."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import cast

from pydantic import JsonValue
from toolang.base.types.message import (
    Part,
    TextPart,
    ReasoningPart,
    ToolCallPart,
    ToolResultPart,
)
from toolang.lang.ast import AwaitStmt
from .events import (
    PartData,
    StepInfo,
    FlowStepInfo,
    FlowOperation,
    ModelStepInfo,
    ToolStepInfo,
    StepSummary,
    EventOutput,
    ControlApplied,
    StepBegin,
    StepEnd,
)
from .types import (
    StepSpec,
    StepReport,
    Output,
    TypedRef,
    ErrorRef,
    ModelStepSpec,
    ToolStepSpec,
    ModelStepReport,
    ToolStepReport,
    ExecStepReport,
    CollectionStepReport,
    AwaitableHandle,
)
from .records import StoredStepSpec, StoredModelStepSpec, StepRecord
from .accounting import (
    selected_usd_cost,
    selected_cost_is_approximate,
)


def part_data(part: Part) -> PartData:
    """Select content or explicit tool receipts, without copying opaque bodies."""
    if isinstance(part, TextPart | ReasoningPart):
        return {"type": part.type, "text": part.text}
    if isinstance(part, ToolCallPart | ToolResultPart):
        identity: dict[str, JsonValue] = {
            "type": part.type,
            "tool_call_id": part.tool_call_id,
            "tool_name": part.tool_name,
            "tool_family": part.tool_family,
        }
        if isinstance(part, ToolResultPart):
            if part.error:
                identity["error"] = part.error
            if part.tool_name in {"runtime__run", "runtime__spawn"}:
                for source, target in (("id", "run"), ("thread", "thread")):
                    if isinstance(value := part.output.get(source), str):
                        identity[target] = value
            if part.tool_name == "runtime__exec":
                identity["control_applied"] = bool(part.output.get("controls"))
            if part.tool_name == "runtime__chdir" and isinstance(
                cwd := part.output.get("cwd"), str
            ):
                identity["cwd"] = cwd
        return cast(PartData, identity)
    # Media Parts have only display fields and actual content, with no native state.
    return cast(PartData, part.to_data())


def _direct_value(value: object) -> JsonValue:
    if isinstance(value, Part):
        return cast(JsonValue, part_data(value))
    if isinstance(value, AwaitableHandle):
        return {"id": value.id, "thread": value.thread}
    if isinstance(value, Mapping):
        return {str(key): _direct_value(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        return [_direct_value(item) for item in value]
    if value is None or isinstance(value, str | int | float | bool):
        return value
    raise TypeError(f"event output requires direct data, got {type(value).__name__}")


def event_output(type_name: str, value: object) -> EventOutput:
    """Project once at the producer boundary, never during serialization."""
    return EventOutput(type_name, _direct_value(value))


def step_info(spec: StepSpec | StoredStepSpec) -> StepInfo:
    """Select presentation facts without assembling a model call or traversing AST bodies."""
    if isinstance(spec, ModelStepSpec | StoredModelStepSpec):
        return ModelStepInfo(spec.model)
    if isinstance(spec, ToolStepSpec):
        name = spec.call.name
        runtime = spec.plugin == "runtime"
        awaiting = runtime and name == "runtime__await"
        launching = runtime and name in {
            "runtime__run",
            "runtime__exec",
            "runtime__spawn",
        }
        target = (
            spec.call.input.get("target" if awaiting else "runnable", "")
            if awaiting or launching
            else ""
        )
        return ToolStepInfo(
            spec.plugin,
            spec.call.name,
            spec.summary,
            target if isinstance(target, str) else "",
            runtime and name == "runtime__run" and spec.call.input.get("async") is True,
        )
    return FlowStepInfo(
        kind=cast(FlowOperation, spec.kind),
        runnable=getattr(spec, "runnable", None),
        handle=getattr(spec, "handle", ""),
        name=getattr(spec, "name", "") or "",
        binding=spec.binding,
        doc=spec.doc,
        asynchronous=getattr(spec, "asynchronous", False),
        count=getattr(spec, "count", None),
        lanes=getattr(spec, "lanes", None),
        position=getattr(spec, "position", None),
        order=getattr(spec, "order", "ascending"),
    )


def step_summary(
    report: StepReport,
    *,
    exec_runnable: str | None = None,
    exec_runnable_kind: str = "",
) -> StepSummary | None:
    if report is None:
        return None
    if isinstance(report, ModelStepReport):
        if report.usage is None and report.cost is None:
            return None
        return StepSummary(
            usage=report.usage,
            cost=selected_usd_cost(report.cost),
            cost_approximate=selected_cost_is_approximate(report.cost),
        )
    if isinstance(report, ToolStepReport):
        return StepSummary(summary=report.summary)
    if isinstance(report, ExecStepReport):
        if exec_runnable is None:
            raise ValueError("exec completion requires its committed runnable")
        return StepSummary(runnable=exec_runnable, runnable_kind=exec_runnable_kind)
    if isinstance(report, CollectionStepReport):
        return StepSummary(
            total_items=report.total_items, output_items=report.output_items
        )
    return StepSummary(
        iterations=report.iterations, termination=report.termination, total=report.total
    )


def direct_output(output: Output | None) -> EventOutput | None:
    if output is None:
        return None
    # A durable output edge alone does not ask clients to materialize a value.
    if isinstance(output.value, TypedRef):
        return None
    return event_output(output.type, output.value)


def step_begin_event(step: StepRecord) -> StepBegin:
    return StepBegin(
        step=step.id,
        run=step.run_id,
        parent=str(step.parent) if step.parent else None,
        index=step.index,
        kind=step.kind,
        spec=step_info(step.spec),
        preceded_by=tuple(
            ControlApplied(str(ref.target), ref.index) for ref in step.preceded_by
        ),
        occurrence=step.occur,
        started_at=step.started_at,
    )


def step_end_event(
    step: StepRecord,
    output: EventOutput | None,
    error: str | None,
    *,
    exec_runnable: str | None = None,
    exec_runnable_kind: str = "",
) -> StepEnd:
    return StepEnd(
        step=step.id,
        run=step.run_id,
        kind=step.kind,
        status=step.status,
        output=output,
        report=step_summary(
            step.report,
            exec_runnable=exec_runnable,
            exec_runnable_kind=exec_runnable_kind,
        ),
        error_source=str(step.error.ref.record)
        if isinstance(step.error, ErrorRef)
        and not (
            isinstance(step.spec, AwaitStmt)
            or isinstance(step.spec, ToolStepSpec)
            and step.spec.plugin == "runtime"
            and step.spec.call.name == "runtime__await"
        )
        else None,
        error=error,
        finished_at=step.finished_at or "",
    )
