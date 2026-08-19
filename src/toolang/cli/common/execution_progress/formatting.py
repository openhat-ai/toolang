"""Pure formatting helpers for script execution progress."""

from __future__ import annotations

import json
import re

from toolang.base.types.message import (
    AudioPart,
    DocumentPart,
    ImagePart,
    Part,
    TextPart,
    ToolResultPart,
    message_text,
)
from toolang.execution.events import StepEnd
from toolang.execution.records import execution_error_message
from toolang.execution.types import (
    ModelStepGiven,
    ModelStepNoted,
    StepGiven,
    StepNoted,
    ToolStepGiven,
)
from toolang.lang.ast import (
    AskStmt,
    DropStmt,
    FlowStmt,
    GatherStmt,
    KeepStmt,
    LetStmt,
    MapStmt,
    RankStmt,
    RepeatStmt,
    RunStmt,
    ScatterStmt,
    SeekStmt,
    SettleStmt,
    StormStmt,
)
from toolang.lang.types import Array

from ..output import parse_utc_timestamp


def integer(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


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
    return f"{compact_count(input_tokens)}/{compact_count(output_tokens)} tokens"


def usage_facts(noted: StepNoted) -> list[str]:
    if not isinstance(noted, ModelStepNoted):
        return []
    facts = []
    if noted.tokens is not None:
        facts.append(token_fact(noted.tokens.input, noted.tokens.output))
    if noted.cost is not None:
        facts.append(f"${noted.cost}")
    return facts


def part_lines(parts: tuple[Part, ...]) -> list[str]:
    lines: list[str] = []
    rendered_text = one_line(
        message_text(tuple(part for part in parts if isinstance(part, TextPart)))
    )
    if rendered_text:
        lines.append(truncate(rendered_text, 160))
    for part in parts:
        if isinstance(part, ImagePart):
            lines.append(f"[image] {part.filename or 'image'}")
        elif isinstance(part, AudioPart):
            lines.append(f"[audio] {part.filename or 'audio'}")
        elif isinstance(part, DocumentPart):
            lines.append(f"[document] {part.filename or 'document'}")
    return lines


def value_summary(value: object) -> str:
    if isinstance(value, str):
        return truncate(one_line(value), 80)
    try:
        return truncate(
            json.dumps(value, ensure_ascii=False, separators=(",", ":")),
            80,
        )
    except TypeError:
        return truncate(str(value), 80)


def output_preview(event: StepEnd) -> str:
    value = " ".join(
        part.text for part in output_parts(event) if isinstance(part, TextPart)
    )
    return truncate(one_line(value), 180)


def step_output_summary(event: StepEnd) -> str:
    """Return one bounded human-readable Step output summary."""

    if text := output_preview(event):
        return text
    if event.output is None:
        return ""
    if event.output.dim == 1:
        return shape_label(event)
    if isinstance(event.output.value, str):
        return truncate(json.dumps(event.output.value, ensure_ascii=False), 80)
    return value_summary(event.output.value)


def model_label(given: StepGiven) -> str:
    return given.model if isinstance(given, ModelStepGiven) else "model"


def tool_label(given: StepGiven) -> str:
    return given.call.name if isinstance(given, ToolStepGiven) else "tool"


def tool_result(event: StepEnd) -> str:
    for part in output_parts(event):
        if not isinstance(part, ToolResultPart):
            continue
        results = part.output.get("results")
        if isinstance(results, list | tuple):
            return f"{len(results)} results"
        code = tool_exit_code(event)
        if code is not None:
            return f"exit {code}"
        if part.error:
            return part.error
    return ""


def tool_output_summary(event: StepEnd) -> str:
    """Return one bounded Tool output summary suitable for one logical row."""

    if result := tool_result(event):
        return result
    for part in output_parts(event):
        if not isinstance(part, ToolResultPart):
            continue
        for key in ("stdout", "stderr"):
            value = part.output.get(key)
            if isinstance(value, str) and value.strip():
                return truncate(one_line(value), 160)
        if part.output:
            return value_summary(part.output)
    return ""


def tool_exit_code(event: StepEnd) -> int | None:
    for part in output_parts(event):
        if not isinstance(part, ToolResultPart):
            continue
        for key in ("exit_code", "returncode", "status_code"):
            if (code := integer(part.output.get(key))) is not None:
                return code
    if error := execution_error_message(event.error):
        match = re.search(r"\b(?:exit|status)\s+(\d+)\b", error)
        if match is not None:
            return int(match.group(1))
    return None


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


def progress_statement_header(statement: FlowStmt) -> str:
    """Return one concise presentation header from a typed Flow statement."""

    if statement.doc and (doc := one_line(statement.doc.strip())):
        return doc

    if isinstance(statement, LetStmt):
        return _statement_words("Set", statement.binding or "value")
    if isinstance(statement, RunStmt):
        action = _named_or_inline(
            statement.runnable,
            named="Run {name}",
            inline="Run the inline task",
        )
    elif isinstance(statement, SeekStmt):
        action = _named_or_inline(
            statement.runnable,
            named=f"Ask {statement.name} to run {{name}}",
            inline=f"Ask {statement.name} for help",
        )
    elif isinstance(statement, AskStmt):
        action = (
            f"Ask {statement.name} for input"
            if statement.name
            else "Ask for human input"
        )
    elif isinstance(statement, ScatterStmt):
        action = _named_or_inline(
            statement.runnable,
            named=f"Expand into {count(statement.count, 'item')} with {{name}}",
            inline=f"Expand into {count(statement.count, 'item')}",
        )
    elif isinstance(statement, StormStmt):
        action = _named_or_inline(
            statement.runnable,
            named=f"Run {{name}} {count(statement.count, 'time')}",
            inline=f"Generate {count(statement.count, 'item')}",
        )
    elif isinstance(statement, GatherStmt):
        action = _named_or_inline(
            statement.runnable,
            named="Combine the items with {name}",
            inline="Combine the items",
        )
    elif isinstance(statement, SettleStmt):
        action = _named_or_inline(
            statement.runnable,
            named="Reduce the items with {name}",
            inline="Reduce the items",
        )
    elif isinstance(statement, MapStmt):
        action = _named_or_inline(
            statement.runnable,
            named="Run {name} for each item",
            inline="Process each item",
        )
    elif isinstance(statement, KeepStmt | DropStmt):
        verb = "Keep" if isinstance(statement, KeepStmt) else "Drop"
        if statement.position is not None and statement.count is not None:
            quantity = "item" if statement.count == 1 else f"{statement.count} items"
            action = f"{verb} the {statement.position} {quantity}"
        else:
            action = _named_or_inline(
                statement.runnable or "",
                named=f"{verb} items selected by {{name}}",
                inline=f"{verb} selected items",
            )
    elif isinstance(statement, RankStmt):
        action = _named_or_inline(
            statement.runnable,
            named="Rank items with {name}",
            inline="Rank the items",
        )
        if statement.selection is not None and statement.limit is not None:
            action += (
                f" and keep the {statement.selection} {count(statement.limit, 'item')}"
            )
    elif isinstance(statement, RepeatStmt):
        if statement.count is not None and statement.runnable is not None:
            return f"Repeat up to {count(statement.count, 'time')}"
        if statement.count is not None:
            return f"Repeat {count(statement.count, 'time')}"
        return "Repeat until complete"
    else:
        raise TypeError(f"unsupported flow statement: {type(statement).__name__}")

    lanes = getattr(statement, "lanes", None)
    if isinstance(lanes, int):
        action += f", up to {lanes} at once"
    if statement.binding == "_":
        return action
    if statement.binding is None:
        return f"{action} without saving the result"
    return f"{action} and save as {statement.binding}"


def progress_until_header(statement: RepeatStmt) -> str:
    """Return the Repeat until boundary label without exposing generated names."""

    runnable = statement.runnable or ""
    return "Check whether to stop" if _generated_runnable(runnable) else runnable


def _named_or_inline(value: str, *, named: str, inline: str) -> str:
    return inline if _generated_runnable(value) else named.format(name=value)


def _generated_runnable(value: str) -> bool:
    return not value or value.startswith("<agic:")


def _statement_words(*values: str | None) -> str:
    return " ".join(value for value in values if value)


def flow_statement(given: StepGiven) -> FlowStmt | None:
    """Return the Flow statement carried directly by one Step given value."""

    return None if isinstance(given, ModelStepGiven | ToolStepGiven) else given


def runnable_label(value: str) -> str:
    """Render generated runnable line numbers as explicit source references."""

    match = re.fullmatch(r"<agic:(\d+)>", value)
    return f"<agic:L{match.group(1)}>" if match is not None else value


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
