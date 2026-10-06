"""Project a synchronous child Run outcome into its one tool reply."""

from collections.abc import Callable
from typing import cast

from toolang.base.types.message import Part
from toolang.base.types.tool import ToolResult
from toolang.lang.types import Array, Value

from ..records import RunRecord
from ..types import ErrorMessage, ErrorRef, value_to_protocol_data
from ..values import parts_from_value


def run_result(
    run: RunRecord,
    resolve: Callable[[object], object],
    resolve_error: Callable[[ErrorMessage | ErrorRef], str],
) -> ToolResult:
    """Return typed result data; child tool-shaped Parts remain nested data."""
    if run.status in {"pending", "running"}:
        raise ValueError(f"Run has no result: {run.id}")
    if run.status != "succeeded":
        return ToolResult(error=resolve_error(run.error) if run.error else run.status)
    if run.output is None:
        return ToolResult()
    value = cast(Value, resolve(run.output.value))
    if isinstance(value, Part):
        parts = parts_from_value(value, content_only=True)
        data = parts[0].to_data() if parts else None
    elif (
        isinstance(value, Array)
        and value
        and all(isinstance(item, Part) for item in value)
    ):
        data = [part.to_data() for part in parts_from_value(value, content_only=True)]
    else:
        data = value_to_protocol_data(value)
    return ToolResult({"type": run.output.type, "value": data})
