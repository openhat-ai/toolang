"""Pure Step-specific execution progress projection."""

from __future__ import annotations

import json

from toolang.base.types.message import (
    TextPart,
    ToolCallPart,
    ToolResultPart,
)
from toolang.execution.events import StepBegin, StepEnd
from toolang.execution.records import local_value_to_data
from toolang.execution.types import LoopStepNoted

from .formatting import one_line, output_parts, tool_label
from .types import ProgressRow, ProgressTone


def live_row(begin: StepBegin, preview: str) -> ProgressRow:
    """Project one non-parallel Step's current replaceable activity."""

    if begin.kind == "model":
        detail = one_line(preview)
        text = f"· thinking… {detail}" if detail else "· thinking…"
    elif begin.kind == "tool":
        text = f"· executing {tool_label(begin.given)}…"
    else:
        text = f"· running {begin.kind}…"
    return ProgressRow(text, "active")


def trace_terminal_rows(
    begin: StepBegin,
    event: StepEnd,
    *,
    error: str,
) -> tuple[ProgressRow, ...]:
    """Project one Model or Tool Step's complete finalized trace."""

    tone = _tone(event.status)
    if begin.kind == "model":
        if event.status == "succeeded":
            return _marked_rows(_model_output_lines(event) or ["completed"], tone)
        if event.status == "failed":
            return (ProgressRow(f"· failed {error}".rstrip(), tone),)
        return (ProgressRow(f"· canceled {error}".rstrip(), tone),)

    label = tool_label(begin.given)
    if event.status == "succeeded":
        rows = [ProgressRow(f"· executed {label}", tone)]
        rows.extend(
            ProgressRow(f"  {line}", tone) for line in _tool_output_lines(event)
        )
        return tuple(rows)
    status = "failed" if event.status == "failed" else "canceled"
    rows = [ProgressRow(f"· {status} {label}", tone)]
    if error:
        rows.extend(ProgressRow(f"  {line}", tone) for line in _split_lines(error))
    return tuple(rows)


def flow_terminal_rows(
    event: StepEnd,
    *,
    error: str,
) -> tuple[ProgressRow, ...]:
    """Project terminal output owned directly by an ordinary Flow Step."""

    tone = _tone(event.status)
    if event.status == "failed":
        return (ProgressRow(f"· {error or 'failed'}", tone),) if error else ()
    if event.status == "canceled":
        return (ProgressRow("· canceled", tone),)
    if event.kind == "run":
        return ()
    return _marked_rows(_flow_output_lines(event), tone)


def loop_terminal_rows(
    event: StepEnd,
    *,
    observed_iterations: int,
    error: str = "",
) -> tuple[ProgressRow, ...]:
    """Project one Loop Step's typed terminal cause."""

    noted = event.noted if isinstance(event.noted, LoopStepNoted) else None
    iterations = noted.iterations if noted is not None else observed_iterations
    termination = (
        noted.termination
        if noted is not None
        else "exhausted"
        if event.status == "succeeded"
        else event.status
    )
    if termination == "exhausted":
        text = f"completed {_count(iterations, 'iteration')}"
    elif termination == "satisfied":
        text = f"condition met after {_count(iterations, 'iteration')}"
    elif termination == "failed":
        text = f"interrupted after {_count(iterations, 'iteration')}"
    else:
        text = f"canceled after {_count(iterations, 'iteration')}"
    tone = _tone(event.status)
    if event.status == "failed" and error:
        return (
            ProgressRow(f"· {error}", tone),
            ProgressRow(f"  {text}", tone),
        )
    return (ProgressRow(f"· {text}", tone),)


def lane_live_text(begin: StepBegin, preview: str) -> str:
    """Project one descendant Step into a compact parallel-lane activity."""

    return live_row(begin, preview).text


def lane_terminal_lines(
    begin: StepBegin,
    event: StepEnd,
    *,
    error: str,
) -> tuple[str, ...]:
    """Project one descendant Step into compact or expandable lane content."""

    rows = trace_terminal_rows(begin, event, error=error)
    if event.status == "failed":
        return tuple(
            row.text[2:] if index else row.text for index, row in enumerate(rows)
        )
    compact = " · ".join(one_line(row.text).strip() for row in rows if row.text)
    return (compact,) if compact else ()


def flow_lane_terminal_lines(
    event: StepEnd,
    *,
    error: str,
    observed_iterations: int = 0,
) -> tuple[str, ...]:
    """Project Flow-owned terminal content without synthesizing leaf activity."""

    if event.kind == "par":
        return ()
    if event.kind == "loop":
        rows = loop_terminal_rows(
            event,
            observed_iterations=observed_iterations,
            error=error,
        )
    else:
        rows = flow_terminal_rows(event, error=error)
    return tuple(
        row.text[2:] if index and row.text.startswith("  ") else row.text
        for index, row in enumerate(rows)
        if row.text
    )


def _model_output_lines(event: StepEnd) -> list[str]:
    lines: list[str] = []
    for part in output_parts(event):
        if isinstance(part, TextPart):
            lines.extend(_split_lines(part.text))
        elif isinstance(part, ToolCallPart):
            name = part.tool_name or part.tool_family or "tool"
            lines.append(f"requested {name}")
        else:
            lines.extend(_json_lines(part.to_data()))
    if lines:
        return lines
    return _flow_output_lines(event)


def _tool_output_lines(event: StepEnd) -> list[str]:
    lines: list[str] = []
    for part in output_parts(event):
        if not isinstance(part, ToolResultPart):
            lines.extend(_json_lines(part.to_data()))
            continue
        if part.error:
            lines.extend(_split_lines(part.error))
        output = dict(part.output)
        textual = [
            value
            for key in ("stdout", "stderr")
            if isinstance((value := output.get(key)), str) and value
        ]
        if textual and set(output).issubset({"stdout", "stderr", "exit_code"}):
            for value in textual:
                lines.extend(_split_lines(value))
        elif output:
            lines.extend(_json_lines(output))
    return lines


def _flow_output_lines(event: StepEnd) -> list[str]:
    if event.output is None:
        return []
    parts = output_parts(event)
    if parts:
        lines: list[str] = []
        for part in parts:
            if isinstance(part, TextPart):
                lines.extend(_split_lines(part.text))
            else:
                lines.extend(_json_lines(part.to_data()))
        return lines
    value = local_value_to_data(event.output.value)
    if isinstance(value, str):
        return _split_lines(value)
    return _json_lines(value)


def _marked_rows(lines: list[str], tone: ProgressTone) -> tuple[ProgressRow, ...]:
    if not lines:
        return ()
    return tuple(
        ProgressRow(f"· {line}" if index == 0 else f"  {line}", tone)
        for index, line in enumerate(lines)
    )


def _split_lines(value: str) -> list[str]:
    return value.splitlines() or ([value] if value else [])


def _json_lines(value: object) -> list[str]:
    try:
        return json.dumps(value, ensure_ascii=False, indent=2).splitlines()
    except TypeError:
        return _split_lines(str(value))


def _count(value: int, noun: str) -> str:
    return f"{value} {noun}{'' if value == 1 else 's'}"


def _tone(status: str) -> ProgressTone:
    if status == "failed":
        return "error"
    if status == "canceled":
        return "warning"
    return "progress"
