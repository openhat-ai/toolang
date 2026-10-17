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
    Usage,
    EventOutput,
    ControlApplied,
    StepBegin,
    StepEnd,
)
from .types import (
    StepGiven,
    StepNoted,
    Output,
    TypedRef,
    ErrorRef,
    ModelStepGiven,
    ToolStepGiven,
    ModelStepNoted,
    ToolStepNoted,
    ExecStepNoted,
    CollectionStepNoted,
    AwaitableHandle,
)
from .records import StoredStepGiven, StoredModelStepGiven, StepRecord
from .accounting import (
    token_meter_quantity,
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


def step_info(given: StepGiven | StoredStepGiven) -> StepInfo:
    """Select presentation facts without assembling a model call or traversing AST bodies."""
    if isinstance(given, ModelStepGiven | StoredModelStepGiven):
        return ModelStepInfo(given.model)
    if isinstance(given, ToolStepGiven):
        name = given.call.name
        runtime = given.plugin == "runtime"
        awaiting = runtime and name == "runtime__await"
        launching = runtime and name in {
            "runtime__run",
            "runtime__exec",
            "runtime__spawn",
        }
        target = (
            given.call.input.get("target" if awaiting else "runnable", "")
            if awaiting or launching
            else ""
        )
        return ToolStepInfo(
            given.plugin,
            given.call.name,
            given.summary,
            target if isinstance(target, str) else "",
            runtime
            and name == "runtime__run"
            and given.call.input.get("async") is True,
        )
    return FlowStepInfo(
        kind=cast(FlowOperation, given.kind),
        runnable=getattr(given, "runnable", None),
        handle=getattr(given, "handle", ""),
        name=getattr(given, "name", "") or "",
        binding=given.binding,
        doc=given.doc,
        asynchronous=getattr(given, "asynchronous", False),
        count=getattr(given, "count", None),
        lanes=getattr(given, "lanes", None),
        position=getattr(given, "position", None),
        order=getattr(given, "order", "ascending"),
    )


def step_summary(noted: StepNoted) -> StepSummary | None:
    if noted is None:
        return None
    if isinstance(noted, ModelStepNoted):
        accounting = noted.accounting
        if accounting is None:
            return None
        return StepSummary(
            usage=Usage(
                accounting.input_tokens,
                accounting.output_tokens,
                token_meter_quantity(accounting, "input.cache_read"),
                token_meter_quantity(accounting, "output.reasoning"),
                selected_usd_cost(accounting),
                selected_cost_is_approximate(accounting),
            )
        )
    if isinstance(noted, ToolStepNoted):
        return StepSummary(summary=noted.summary)
    if isinstance(noted, ExecStepNoted):
        return StepSummary(runnable=noted.runnable)
    if isinstance(noted, CollectionStepNoted):
        return StepSummary(
            total_items=noted.total_items, output_items=noted.output_items
        )
    return StepSummary(
        iterations=noted.iterations, termination=noted.termination, total=noted.total
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
        given=step_info(step.given),
        preceded_by=tuple(
            ControlApplied(str(ref.target), ref.index) for ref in step.preceded_by
        ),
        occurrence=step.occur,
        started_at=step.started_at,
    )


def step_end_event(
    step: StepRecord, output: EventOutput | None, error: str | None
) -> StepEnd:
    return StepEnd(
        step=step.id,
        run=step.run_id,
        kind=step.kind,
        status=step.status,
        output=output,
        noted=step_summary(step.noted),
        error_source=str(step.error.ref.record)
        if isinstance(step.error, ErrorRef)
        and not (
            isinstance(step.given, AwaitStmt)
            or isinstance(step.given, ToolStepGiven)
            and step.given.plugin == "runtime"
            and step.given.call.name == "runtime__await"
        )
        else None,
        error=error,
        finished_at=step.finished_at or "",
    )
