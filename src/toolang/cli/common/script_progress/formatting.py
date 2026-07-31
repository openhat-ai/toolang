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
    Percept,
    TextPart,
    ToolResultPart,
    message_text,
)
from toolang.execution.events import StepBegin, StepEnd

from ..output import parse_utc_timestamp


def mapping(value: object) -> Mapping[str, Any]:
    return cast(Mapping[str, Any], value) if isinstance(value, Mapping) else {}


def text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def integer(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def status_label(status: str) -> str:
    return "succeeded" if status == "finished" else status


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
    return (
        f"{compact_count(input_tokens)}/{compact_count(output_tokens)} tokens"
    )


def usage_facts(noted: Mapping[str, Any]) -> list[str]:
    usage = mapping(noted.get("usage"))
    input_tokens = integer(usage.get("input_tokens"))
    output_tokens = integer(usage.get("output_tokens"))
    if input_tokens is None and output_tokens is None:
        return []
    return [token_fact(input_tokens or 0, output_tokens or 0)]


def percept_lines(percept: Percept) -> list[str]:
    lines: list[str] = []
    rendered_text = one_line(
        message_text(tuple(part for part in percept if isinstance(part, TextPart)))
    )
    if rendered_text:
        lines.append(truncate(rendered_text, 160))
    for part in percept:
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
        part.text for part in event.output if isinstance(part, TextPart)
    )
    return truncate(one_line(value), 180)


def model_label(given: Mapping[str, Any]) -> str:
    model = mapping(given.get("model"))
    return text(model.get("ref")) or text(model.get("model")) or "model"


def tool_label(given: Mapping[str, Any]) -> str:
    return text(given.get("tool")) or "tool"


def tool_result(event: StepEnd) -> str:
    for part in event.output:
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
    for part in event.output:
        if not isinstance(part, ToolResultPart):
            continue
        for key in ("exit_code", "returncode", "status_code"):
            if (code := integer(part.output.get(key))) is not None:
                return code
    if event.error:
        match = re.search(r"\b(?:exit|status)\s+(\d+)\b", event.error)
        if match is not None:
            return int(match.group(1))
    return None


def active_step_label(event: StepBegin) -> str:
    if event.kind == "model":
        return "thinking…"
    if event.kind == "tool":
        return f"executing {tool_label(event.given)}…"
    return f"{event.kind}…"


def completed_step_label(begin: StepBegin, event: StepEnd) -> str:
    if event.status != "finished":
        return status_label(event.status)
    if begin.kind == "model":
        return output_preview(event) or "model completed"
    if begin.kind == "tool":
        return tool_result(event) or f"{tool_label(begin.given)} completed"
    return f"{begin.kind} completed"


def statement_title(given: Mapping[str, Any]) -> str:
    statement = text(given.get("statement")) or "statement"
    target = statement_target(given)
    amount = integer(given.get("count"))
    position = text(given.get("position"))
    limit = text(given.get("limit"))
    parallelism = integer(given.get("par"))
    parts = [statement]
    if statement in {"scatter", "storm", "repeat"} and amount is not None:
        parts.append(str(amount))
    if position:
        parts.append(position)
        if amount is not None:
            parts.append(str(amount))
    if target:
        parts.append(target)
    details: list[str] = []
    if limit:
        details.append(f"{limit} {amount}" if amount is not None else limit)
    if parallelism is not None:
        details.append(f"par {parallelism}")
    return " · ".join((" ".join(parts), *details))


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


def statement_index(step: str) -> int:
    try:
        return int(step.rsplit("/", 1)[-1])
    except ValueError:
        return 0


def shape_label(event: StepEnd) -> str:
    shape = text(event.noted.get("shape"))
    items = integer(event.noted.get("items"))
    if shape == "list" or items is not None:
        return f"{items}-item list" if items is not None else "list"
    if shape == "item" or event.output:
        return "1 item"
    return ""


def binding_action(given: Mapping[str, Any]) -> str:
    if "binding" not in given:
        return ""
    binding = given.get("binding")
    if binding is None:
        return "Discard result"
    return f"Save result to {binding}"


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
    if statement == "run":
        effect = "returned by one run"
    elif statement == "scatter":
        effect = "scattered from 1 item"
    elif statement == "storm":
        effect = f"produced by {source_count or items or 0} runs"
    elif statement == "gather":
        effect = _list_source("gathered from", source_items)
    elif statement == "settle":
        effect = _list_source("settled from", source_items)
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
    elif statement == "repeat":
        effect = "completed repeated execution"
    elif statement == "let":
        effect = "perceived from authored content"
    else:
        effect = "produced"
    return " · ".join(value for value in (shape, effect) if value)


def runtime_failure(event: StepBegin) -> bool:
    return event.kind == "system" and event.given.get("runtime") == "failure"


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
        verb = "kept" if statement == "keep" else "dropped"
        if statement == "keep":
            actual = selected or 0
        elif source_items is not None:
            actual = source_items - (selected or 0)
        else:
            actual = count_value
        suffix = (
            f" of {source_items} items"
            if source_items is not None
            else ""
        )
        return f"{verb} {position} {actual}{suffix}"
    if statement == "keep":
        return _selected_count("kept", selected, source_items)
    if statement == "drop":
        dropped = (
            source_items - (selected or 0)
            if source_items is not None
            else None
        )
        return _selected_count("dropped", dropped, source_items)
    return "filtered"


def _rank_result(
    given: Mapping[str, Any],
    *,
    selected: int | None,
    source_items: int | None,
) -> str:
    limit = text(given.get("limit"))
    if limit:
        return _selected_count(
            f"selected {limit}",
            selected,
            source_items,
        )
    count_value = source_items if source_items is not None else selected
    return (
        f"ranked {count_value} items"
        if count_value is not None
        else "ranked"
    )


def _list_source(verb: str, source_items: int | None) -> str:
    if source_items is None:
        return f"{verb} a list"
    return f"{verb} a {source_items}-item list"


def _selected_count(
    verb: str,
    selected: int | None,
    source_items: int | None,
) -> str:
    actual = selected or 0
    if source_items is None:
        return f"{verb} {actual} items"
    return f"{verb} {actual} of {source_items} items"
