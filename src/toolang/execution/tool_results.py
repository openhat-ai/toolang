"""Runtime control receipts and replies for intercepted workspace calls."""

from collections.abc import Callable
from dataclasses import asdict
from pathlib import PurePosixPath

from toolang.base.schemas import (
    CompactControlSummary,
    ControlSummary,
    RecallControlSummary,
    ReloadControlSummary,
)
from toolang.base.types.message import ToolCallPart, ToolResultPart
from toolang.base.types.run import ToolCall

from .records import (
    CompactControlPayload,
    RecallControlPayload,
    ReloadControlPayload,
    StepRecord,
)
from .types import (
    ControlRef,
    RulesRecallTarget,
    ToolStepGiven,
    TypedRef,
    ErrorMessage,
    ErrorRef,
)


def control_summary(
    ref: ControlRef,
    payload: RecallControlPayload | ReloadControlPayload | CompactControlPayload,
) -> ControlSummary:
    """Project durable facts without reading State or copying recalled content."""

    if isinstance(payload, RecallControlPayload):
        target = asdict(payload.target)
        if isinstance(payload.target, RulesRecallTarget):
            target["path"] = str(PurePosixPath(payload.target.path) / "AGENTS.md")
        return RecallControlSummary(
            ref=str(ref), target=target, revision=payload.revision
        )
    if isinstance(payload, ReloadControlPayload):
        return ReloadControlSummary(ref=str(ref), state=payload.state)
    return CompactControlSummary(ref=str(ref), horizon=str(payload.horizon))


def workspace_reply(
    call: ToolCall, *, status: str = "succeeded", error: str | None = None
) -> ToolResultPart:
    """Build the same error-only reply online and from an unrecorded history tail."""

    if status == "canceled":
        error = "canceled"
    elif status != "succeeded":
        error = error or "interrupted"
    return ToolResultPart(
        tool_call_id=call.tool_call_id,
        call_id=call.call_id,
        tool_name=call.name,
        tool_family=call.name,
        error=(
            f"Workspace rules could not be loaded; operation not executed: {error}"
            if error
            else "Workspace rules were just loaded. This operation was not executed; "
            "please retry if it complies with them."
        ),
    )


def workspace_reply_from_step(
    step: StepRecord, resolve: Callable[[object], object]
) -> ToolResultPart | None:
    """Recover an intercepted call's reply from honor's durable dependency/outcome."""

    given = step.given
    if not (
        isinstance(given, ToolStepGiven)
        and given.trigger == "runtime"
        and given.call.name == "_toolang__honor"
        and step.input
    ):
        return None
    source = resolve(TypedRef(step.input[0], "Part"))
    if not isinstance(source, ToolCallPart):
        raise TypeError("honor input must reference the original ToolCallPart")
    error = None
    if step.output is not None and isinstance(step.output.local.value, ToolResultPart):
        error = step.output.local.value.error
    elif isinstance(step.error, ErrorMessage):
        error = step.error.message
    elif isinstance(step.error, ErrorRef):
        error = str(resolve(step.error.ref))
    return workspace_reply(
        ToolCall(
            source.tool_call_id,
            source.call_id or source.tool_call_id,
            source.tool_name,
            dict(source.input),
        ),
        status=step.status,
        error=error,
    )
