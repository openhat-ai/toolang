"""Normalize native Flow and Tool Steps for presentation, without Store reads."""

from toolang.base.types.message import ToolResultPart
from toolang.execution.events import StepBegin, StepEnd
from toolang.execution.types import ExecStepNoted, ModelStepGiven, ToolStepGiven

from .formatting import output_parts, runnable_label
from .types import StepOperation


_RUNTIME_MARKERS = {"spawn", "pick", "honor", "compact"}
_TOOL_OPERATIONS: dict[str, str] = {
    f"_toolang__{name}": name
    for name in ("run", "spawn", "await", "exec", "chdir", "pick", "honor", "compact")
}
# Preserve the existing presentation reader for the former tool spelling.
_TOOL_OPERATIONS["_toolang__execute"] = "exec"


def runtime_operation_name(tool_name: str) -> str | None:
    """Share wire-name recognition with UI consumers of result-only events."""

    return _TOOL_OPERATIONS.get(tool_name)


def normalize_operation(
    begin: StepBegin, *, owner_is_agic: bool = False
) -> StepOperation:
    """Identify the operation once at StepBegin; preserve source-specific styling."""

    given = begin.given
    if isinstance(given, ToolStepGiven):
        name = runtime_operation_name(given.call.name)
        if given.plugin != "_toolang" and name not in {"run", "exec"}:
            name = None
        # Keep existing short-name recognition for runtime progress summaries.
        if given.plugin == "_toolang" and given.call.name in _RUNTIME_MARKERS:
            name = given.call.name
        if name == "run" and given.call.input.get("async") is True:
            name = "async run"
        return StepOperation(
            begin,
            name or "tool",
            "tool",
            tool=given,
            runnable=runnable_label(
                given.call.input.get("target" if name == "await" else "runnable")
            ),
            run_scope=name == "run",
            tool_marker="✧"
            if given.plugin == "_toolang" and name in _RUNTIME_MARKERS
            else "›",
        )
    if isinstance(given, ModelStepGiven):
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

    if isinstance(end.noted, ExecStepNoted):
        return end.noted.runnable
    if (
        operation.name == "exec"
        and operation.source == "tool"
        and any(
            isinstance(part, ToolResultPart)
            and part.error is None
            and bool(part.output.get("controls"))
            for part in output_parts(end)
        )
    ):
        return operation.runnable or "runnable"
    return None
