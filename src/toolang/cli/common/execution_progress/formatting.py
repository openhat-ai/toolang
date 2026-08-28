"""Pure formatting helpers for script execution progress."""

from __future__ import annotations

from toolang.base.types.message import (
    Part,
    ToolResultPart,
)
from toolang.execution.events import StepEnd
from toolang.execution.types import (
    HandoffStepGiven,
    ModelStepGiven,
    StepGiven,
    ToolStepGiven,
)
from toolang.lang.ast import FlowStmt
from toolang.lang.types import Array
from wcwidth import wcwidth, wcswidth

from ..output import parse_utc_timestamp


def status_label(status: str) -> str:
    return status


def one_line(value: str) -> str:
    return " ".join(value.split())


def truncate(value: str, width: int) -> str:
    if display_width(value) <= width:
        return value
    if width <= 0:
        return ""
    prefix = _cell_prefix(value, width - 1)
    return f"{prefix.rstrip()}…"


def wrap_display(value: str, width: int) -> list[str]:
    """Wrap one normalized line without exceeding terminal cell width."""

    if not value or width <= 0:
        return [value]
    lines: list[str] = []
    remaining = value
    while display_width(remaining) > width:
        candidate = _cell_prefix(remaining, width)
        if not candidate:
            candidate = remaining[0]
        split_at = candidate.rfind(" ")
        if split_at > 0:
            lines.append(candidate[:split_at].rstrip())
            remaining = remaining[split_at + 1 :].lstrip()
        else:
            lines.append(candidate)
            remaining = remaining[len(candidate) :].lstrip()
    lines.append(remaining)
    return lines


def display_width(value: str) -> int:
    """Return the terminal cell width of text, tolerating control characters."""

    measured = wcswidth(value)
    if measured >= 0:
        return measured
    return sum(max(wcwidth(char), 0) for char in value)


def split_hanging_prefix(value: str) -> tuple[str, str]:
    """Split one progress row into its fixed prefix and wrappable content."""

    if value.startswith("• "):
        return "• ", value[2:]
    lane_marker = value.find("| • ")
    if lane_marker >= 0:
        content_start = lane_marker + len("| • ")
        return value[:content_start], value[content_start:]
    indent = len(value) - len(value.lstrip())
    return value[:indent], value[indent:]


def _cell_prefix(value: str, width: int) -> str:
    if width <= 0:
        return ""
    used = 0
    end = 0
    for end, char in enumerate(value, start=1):
        char_width = max(wcwidth(char), 0)
        if used + char_width > width:
            return value[: end - 1]
        used += char_width
    return value[:end]


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
    minutes, seconds = divmod(round(duration), 60)
    return f"{minutes}m {seconds:02d}s"


def compact_count(value: int) -> str:
    for threshold, suffix in ((1_000_000, "m"), (1_000, "k")):
        if value >= threshold:
            rendered = value / threshold
            return f"{rendered:.1f}".rstrip("0").rstrip(".") + suffix
    return str(value)


def token_fact(
    input_tokens: int,
    output_tokens: int,
    *,
    cache_read_tokens: int | None = None,
) -> str:
    cache = ""
    if cache_read_tokens is not None and input_tokens > 0:
        ratio = cache_read_tokens / input_tokens * 100
        cache = f"({ratio:.1f}%)"
    return f"↑{compact_count(input_tokens)}{cache} ↓{compact_count(output_tokens)}"


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

    return (
        None
        if isinstance(given, ModelStepGiven | ToolStepGiven | HandoffStepGiven)
        else given
    )


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
