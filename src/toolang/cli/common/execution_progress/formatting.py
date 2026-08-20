"""Pure formatting helpers for script execution progress."""

from __future__ import annotations

from toolang.base.types.message import (
    Part,
    ToolResultPart,
)
from toolang.execution.events import StepEnd
from toolang.execution.types import (
    ModelStepGiven,
    StepGiven,
    ToolStepGiven,
)
from toolang.lang.ast import FlowStmt
from toolang.lang.types import Array

from ..output import parse_utc_timestamp


def status_label(status: str) -> str:
    return status


def one_line(value: str) -> str:
    return " ".join(value.split())


def truncate(value: str, width: int) -> str:
    return value if len(value) <= width else f"{value[: max(width - 1, 0)]}…"


def count(value: int, noun: str) -> str:
    return f"{value} {noun}{'' if value == 1 else 's'}"


def elapsed(started_at: str, finished_at: str) -> str:
    if not started_at or not finished_at:
        return ""
    try:
        started = parse_utc_timestamp(started_at)
        finished = parse_utc_timestamp(finished_at)
        if started is None or finished is None:
            return ""
        duration = max(
            0.0,
            (finished - started).total_seconds(),
        )
    except (TypeError, ValueError):
        return ""
    if duration < 1:
        return f"{round(duration * 1000)}ms"
    if duration < 60:
        return f"{duration:.1f}s"
    minutes = int(duration // 60)
    seconds = round(duration % 60)
    return f"{minutes}m {seconds:02d}s"


def compact_count(value: int) -> str:
    for threshold, suffix in ((1_000_000, "m"), (1_000, "k")):
        if value >= threshold:
            rendered = value / threshold
            return f"{rendered:.1f}".rstrip("0").rstrip(".") + suffix
    return str(value)


def token_fact(input_tokens: int, output_tokens: int) -> str:
    return f"↑{compact_count(input_tokens)} ↓{compact_count(output_tokens)}"


def tool_label(given: StepGiven) -> str:
    return given.call.name if isinstance(given, ToolStepGiven) else "tool"


def output_parts(event: StepEnd) -> tuple[Part, ...]:
    """Return message parts carried by one typed step output."""

    if event.output is None:
        return ()
    value = event.output.value
    if isinstance(value, ToolResultPart):
        return (value,)
    if isinstance(value, Array | tuple | list):
        return tuple(part for part in value if isinstance(part, Part))
    return ()


def flow_statement(given: StepGiven) -> FlowStmt | None:
    """Return the Flow statement carried directly by one Step given value."""

    return None if isinstance(given, ModelStepGiven | ToolStepGiven) else given


def shape_label(event: StepEnd) -> str:
    if event.output is None:
        return ""
    items = output_item_count(event)
    if event.output.dim == 1:
        return f"{items}-item list" if items is not None else "list"
    return "1 item"


def output_item_count(event: StepEnd) -> int | None:
    """Return a concrete output count without resolving pointer-backed values."""

    if event.output is None:
        return None
    value = event.output.value
    if event.output.dim == 1 and isinstance(value, Array | tuple | list):
        return len(value)
    return 1 if event.output.dim == 0 else None
