"""Pure formatting helpers for script execution progress."""

from __future__ import annotations

from toolang.base.types.message import (
    Part,
    part_from_data,
)
from toolang.execution.events import StepEnd, ToolResultData
from toolang.lang.types import authored_type
from typing import cast
from pydantic import JsonValue
from toolang.lang.types import Array, Value, display_runnable_ref
from wcwidth import wcwidth, wcswidth


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

    for marker in ("• ", "› ", "✧ "):
        if value.startswith(marker):
            return marker, value[2:]
        lane_marker = value.find(f"| {marker}")
        if lane_marker >= 0:
            content_start = lane_marker + len(f"| {marker}")
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


def runnable_label(value: object) -> str:
    """Format a runnable target already extracted by operation normalization."""

    if not isinstance(value, str):
        return ""
    safe = "".join(character if character.isprintable() else " " for character in value)
    safe = display_runnable_ref(safe, surface="progress")
    return one_line(safe)[:240]


def output_parts(event: StepEnd) -> tuple[Part, ...]:
    """Return message parts carried by one typed step output."""

    if event.output is None:
        return ()
    kind = authored_type(event.output.type)
    value = event.output.value
    if kind == "Part" and isinstance(value, dict):
        return (part_from_data(value),)
    if kind == "Part[]" and isinstance(value, list):
        return tuple(part_from_data(part) for part in value if isinstance(part, dict))
    return ()


def tool_result_data(event: StepEnd) -> tuple[ToolResultData, ...]:
    """Read explicit client receipts, without rebuilding execution results."""
    if event.output is None or authored_type(event.output.type) not in {
        "Part",
        "Part[]",
    }:
        return ()
    value = event.output.value
    parts = value if isinstance(value, list) else [value]
    return tuple(
        cast(ToolResultData, part)
        for part in parts
        if isinstance(part, dict) and part.get("type") == "tool_result"
    )


def display_value(value: JsonValue, kind: str) -> Value:
    """Decode explicitly typed Parts only at the presentation boundary."""
    kind = authored_type(kind)
    if kind.split("[", 1)[0] != "Part":
        return value
    if kind.endswith("[]") and isinstance(value, list):
        return [display_value(item, kind[:-2]) for item in value]
    if isinstance(value, dict):
        return part_from_data(value)
    return value


def shape_label(event: StepEnd) -> str:
    if event.output is None:
        return ""
    items = output_item_count(event)
    if event.output.type.endswith("[]") or isinstance(
        event.output.value, Array | tuple | list
    ):
        return f"{items}-item list" if items is not None else "list"
    return "1 item"


def output_item_count(event: StepEnd) -> int | None:
    """Return a concrete output count without resolving pointer-backed values."""

    if event.output is None:
        return None
    value = event.output.value
    if isinstance(value, Array | tuple | list):
        return len(value)
    return None if event.output.type.endswith("[]") else 1
