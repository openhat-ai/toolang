"""Pure formatting helpers for script execution progress."""

from __future__ import annotations

from collections.abc import Mapping
import json
import re
from typing import Any, cast

from toolang.base.types.message import (
    AudioPart,
    DocumentPart,
    ImagePart,
    Part,
    TextPart,
    ToolResultPart,
    message_text,
)
from toolang.execution.events import StepBegin, StepEnd
from toolang.execution.records import execution_error_message
from toolang.execution.types import StepPath
from toolang.lang.types import Array

from ..output import parse_utc_timestamp


def mapping(value: object) -> Mapping[str, Any]:
    return cast(Mapping[str, Any], value) if isinstance(value, Mapping) else {}


def text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


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


def usage_facts(noted: Mapping[str, Any]) -> list[str]:
    tokens = mapping(noted.get("tokens"))
    input_tokens = integer(tokens.get("input"))
    output_tokens = integer(tokens.get("output"))
    if input_tokens is None and output_tokens is None:
        return []
    return [token_fact(input_tokens or 0, output_tokens or 0)]


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


def model_label(given: Mapping[str, Any]) -> str:
    model = mapping(given.get("model"))
    return text(model.get("ref")) or text(model.get("model")) or "model"


def tool_label(given: Mapping[str, Any]) -> str:
    return text(given.get("tool")) or "tool"


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


def active_step_label(event: StepBegin) -> str:
    if event.kind == "model":
        return "thinking…"
    if event.kind == "tool":
        return f"executing {tool_label(event.given)}…"
    return f"{event.kind}…"


def completed_step_label(begin: StepBegin, event: StepEnd) -> str:
    if event.status != "succeeded":
        return status_label(event.status)
    if begin.kind == "model":
        return output_preview(event) or "model completed"
    if begin.kind == "tool":
        return tool_result(event) or f"{tool_label(begin.given)} completed"
    return f"{begin.kind} completed"


def statement_head(given: Mapping[str, Any]) -> str:
    source = mapping(given.get("source"))
    return text(source.get("head")) or "statement"


def statement_target(given: Mapping[str, Any]) -> str:
    target = (
        text(given.get("runnable"))
        or text(given.get("predicate"))
        or text(given.get("scorer"))
        or text(given.get("agent"))
    )
    return runnable_label(target)


def runnable_label(value: str) -> str:
    """Render generated runnable line numbers as explicit source references."""

    match = re.fullmatch(r"<agic:(\d+)>", value)
    return f"<agic:L{match.group(1)}>" if match is not None else value


def statement_index(step: StepPath) -> int:
    return step.index


def shape_label(event: StepEnd) -> str:
    shape = text(event.noted.get("shape"))
    items = integer(event.noted.get("items"))
    if shape == "list" or items is not None:
        return f"{items}-item list" if items is not None else "list"
    if shape == "item" or event.output:
        return "1 item"
    return ""


def statement_result_level(given: Mapping[str, Any]) -> int | None:
    """Return the minimum verbosity for one successful statement result."""

    statement = text(given.get("statement"))
    if statement in {"run", "repeat"}:
        return None
    if statement in {"scatter", "keep", "drop"}:
        return 0
    if statement == "rank" and text(given.get("limit")):
        return 0
    return 2


def statement_result(
    given: Mapping[str, Any],
    event: StepEnd,
    *,
    source_items: int | None,
) -> str:
    shape = shape_label(event)
    statement = text(given.get("statement"))
    items = integer(event.noted.get("items"))
    source_count = integer(given.get("count"))
    if statement in {"run", "repeat"}:
        return ""
    if statement == "scatter":
        effect = "scattered from 1 item"
    elif statement == "storm":
        effect = f"produced by {source_count or items or 0} runs"
    elif statement == "gather":
        effect = _list_source("gathered from", source_items)
    elif statement == "settle":
        effect = _list_source("reduced from", source_items)
    elif statement == "map":
        effect = _list_source("mapped from", source_items)
    elif statement in {"keep", "drop"}:
        effect = _selection_result(
            given,
            selected=items,
            source_items=source_items,
        )
    elif statement == "rank":
        effect = _rank_result(
            given,
            selected=items,
            source_items=source_items,
        )
    elif statement == "let":
        effect = "perceived from authored content"
    else:
        effect = "produced"
    if not shape:
        return effect
    binding = given.get("binding", "_")
    result = f"{shape} discarded" if binding is None else f"{shape} saved to {binding}"
    return " · ".join(value for value in (result, effect) if value)


def _selection_result(
    given: Mapping[str, Any],
    *,
    selected: int | None,
    source_items: int | None,
) -> str:
    statement = text(given.get("statement"))
    position = text(given.get("position"))
    count_value = integer(given.get("count"))
    if position and count_value is not None:
        actual = selected or 0
        location = "start" if position == "first" else "end"
        count_label = _fraction(actual, source_items)
        if statement == "keep":
            return f"{count_label} items kept from the {location}"
        return f"{count_label} items retained after dropping from the {location}"
    if statement == "keep":
        return f"{_fraction(selected, source_items)} items kept"
    if statement == "drop":
        return f"{_fraction(selected, source_items)} items retained"
    return "filtered"


def _rank_result(
    given: Mapping[str, Any],
    *,
    selected: int | None,
    source_items: int | None,
) -> str:
    limit = text(given.get("limit"))
    if limit:
        return f"{limit} {_fraction(selected, source_items)} items selected"
    return "ranked"


def _list_source(
    verb: str,
    source_items: int | None,
) -> str:
    if source_items is None:
        return f"{verb} a list"
    return f"{verb} {count(source_items, 'item')}"


def _fraction(selected: int | None, source_items: int | None) -> str:
    actual = selected or 0
    return f"{actual}/{source_items}" if source_items is not None else str(actual)
