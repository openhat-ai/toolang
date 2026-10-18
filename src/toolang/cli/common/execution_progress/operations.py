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

    given = begin.given
    if isinstance(given, ToolStepInfo):
        name = runtime_operation_name(given.name) if given.plugin == "runtime" else None
        if name == "run" and given.asynchronous:
            name = "async run"
        return StepOperation(
            begin,
            name or "tool",
            "tool",
            tool=given,
            runnable=runnable_label(given.target),
            run_scope=name == "run",
            tool_marker="✧" if name in {"pick", "honor", "compact"} else "›",
        )
    if isinstance(given, ModelStepInfo):
        return StepOperation(begin, "model", "model")
    return StepOperation(
        begin,
        "async run" if getattr(given, "asynchronous", False) else given.kind,
        "flow",
        statement=given,
        runnable=runnable_label(
            getattr(given, "runnable", getattr(given, "handle", ""))
        ),
        run_scope=begin.kind == "run"
        and owner_is_agic
        and not getattr(given, "asynchronous", False),
    )


def committed_exec_target(operation: StepOperation, end: StepEnd) -> str | None:
    """Read one committed handoff from either native exec or its tool reply."""

    if end.noted is not None and end.noted.runnable is not None:
        return end.noted.runnable
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
