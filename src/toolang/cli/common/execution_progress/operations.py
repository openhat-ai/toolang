"""Normalize native Flow and Tool Steps for presentation, without Store reads."""

from toolang.execution.events import ToolStepInfo, ModelStepInfo
from toolang.execution.events import StepBegin, StepEnd
from toolang.execution.operations import (
    runtime_operation_name as runtime_operation_name,
)

from .formatting import tool_result_data, runnable_label
from .types import StepOperation


def normalize_operation(
    begin: StepBegin, *, owner_is_agic: bool = False
) -> StepOperation:
    """Identify the operation once at StepBegin; preserve source-specific styling."""

    spec = begin.spec
    if isinstance(spec, ToolStepInfo):
        name = runtime_operation_name(spec.name) if spec.plugin == "runtime" else None
        if name == "run" and spec.asynchronous:
            name = "async run"
        return StepOperation(
            begin,
            name or "tool",
            "tool",
            tool=spec,
            runnable=runnable_label(spec.target),
            run_scope=name == "run",
            tool_marker="✧" if name in {"pick", "honor", "compact"} else "›",
        )
    if isinstance(spec, ModelStepInfo):
        return StepOperation(begin, "model", "model")
    return StepOperation(
        begin,
        "async run" if getattr(spec, "asynchronous", False) else spec.kind,
        "flow",
        statement=spec,
        runnable=runnable_label(getattr(spec, "runnable", getattr(spec, "handle", ""))),
        run_scope=begin.kind == "run"
        and owner_is_agic
        and not getattr(spec, "asynchronous", False),
    )


def committed_exec_target(operation: StepOperation, end: StepEnd) -> str | None:
    """Read one committed handoff from either native exec or its tool reply."""

    if end.report is not None and end.report.runnable is not None:
        from toolang.lang.types import display_runnable_ref

        return display_runnable_ref(
            end.report.runnable, kind=end.report.runnable_kind, surface="progress"
        )
    if (
        operation.name == "exec"
        and operation.source == "tool"
        and any(
            part.get("error") is None and part.get("control_applied") is True
            for part in tool_result_data(end)
        )
    ):
        return operation.runnable or "runnable"
    return None
