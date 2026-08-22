"""Pure Step-specific execution progress projection."""

from __future__ import annotations

import json

from toolang.base.types.message import (
    TextPart,
    ToolCallPart,
    ToolResultPart,
)
from toolang.execution.events import StepBegin, StepEnd
from toolang.execution.types import (
    CollectionStepNoted,
    LoopStepNoted,
    ToolStepGiven,
    ToolStepNoted,
)
from toolang.execution.values import parts_from_local
from toolang.lang.ast import (
    DropStmt,
    FlowStmt,
    KeepStmt,
    MapStmt,
    RankStmt,
    RepeatStmt,
    SettleStmt,
    StormStmt,
)

from .formatting import one_line, output_parts, tool_label
from .types import ProgressRow, ProgressTone


def live_row(begin: StepBegin, preview: str) -> ProgressRow:
    """Project one compact Step activity for a parallel lane."""

    if begin.kind == "model":
        detail = one_line(preview)
        text = f"• {detail}" if detail else "• thinking"
    elif begin.kind == "tool":
        summary = (
            begin.given.summary
            if isinstance(begin.given, ToolStepGiven) and begin.given.summary
            else f"executing {tool_label(begin.given)}"
        )
        text = f"• {summary}"
        return ProgressRow(text, "active", surface="tool_summary")
    else:
        text = f"• running {begin.kind}"
    return ProgressRow(text, "active")


def trace_live_rows(
    begin: StepBegin,
    preview: str,
    *,
    marker_committed: bool = False,
    gap_before: bool = False,
) -> tuple[ProgressRow, ...]:
    """Project replaceable Trace activity or one Markdown source tail."""

    if begin.kind != "model" or not preview:
        if begin.kind == "model" and marker_committed:
            return ()
        return (live_row(begin, preview),)
    return (
        ProgressRow(
            preview,
            "normal",
            wrap_live=True,
            format="markdown",
            prefix="  " if marker_committed else "• ",
            gap_before=gap_before,
        ),
    )


def trace_terminal_rows(
    begin: StepBegin,
    event: StepEnd,
    *,
    error: str,
    include_model_text: bool = True,
) -> tuple[ProgressRow, ...]:
    """Project one Model or Tool Step's complete terminal trace."""

    tone = _tone(event.status)
    if begin.kind == "model":
        if event.status == "succeeded":
            return _marked_rows(
                _model_output_lines(event, include_text=include_model_text)
                or ([] if not include_model_text else ["completed"]),
                "normal",
            )
        if event.status == "failed":
            return _error_rows("failed", error, tone)
        return _error_rows("canceled", error, tone)

    label = tool_label(begin.given)
    summary = event.noted.summary if isinstance(event.noted, ToolStepNoted) else ""
    if event.status == "succeeded":
        rows = [
            ProgressRow(
                f"• {summary or f'executed {label}'}",
                tone,
                surface="tool_summary",
            )
        ]
        rows.extend(
            ProgressRow(f"  {line}", tone, surface="tool_detail")
            for line in _tool_output_lines(event)
        )
        return tuple(rows)
    status = "failed" if event.status == "failed" else "canceled"
    rows = [
        ProgressRow(
            f"• {summary or f'{status} {label}'}",
            tone,
            surface="tool_summary",
        )
    ]
    if error:
        rows.extend(
            ProgressRow(
                f"  {line}",
                tone,
                surface="tool_detail" if event.status == "failed" else "none",
            )
            for line in _split_lines(error.strip())
        )
    return tuple(rows)


def flow_terminal_rows(
    event: StepEnd,
    *,
    error: str,
) -> tuple[ProgressRow, ...]:
    """Project terminal output owned directly by an ordinary Flow Step."""

    tone = _tone(event.status)
    if event.status == "failed":
        return flow_error_rows(error, tone=tone)
    if event.status == "canceled":
        return (ProgressRow("• canceled", tone),)
    if event.kind == "run":
        return ()
    return _marked_rows(_flow_output_lines(event), "normal")


def loop_terminal_rows(
    event: StepEnd,
    *,
    statement: FlowStmt,
    observed_iterations: int,
    error: str = "",
) -> tuple[ProgressRow, ...]:
    """Project one Loop Step's typed terminal cause."""

    noted = event.noted if isinstance(event.noted, LoopStepNoted) else None
    iterations = noted.iterations if noted is not None else observed_iterations
    total = noted.total if noted is not None else None
    if total is None and isinstance(statement, RepeatStmt):
        total = statement.count
    termination = (
        noted.termination
        if noted is not None
        else "exhausted"
        if event.status == "succeeded"
        else event.status
    )
    if isinstance(statement, SettleStmt):
        text = _settle_terminal_text(iterations, total, termination)
    else:
        text = _repeat_terminal_text(
            iterations,
            total,
            termination,
            has_condition=isinstance(statement, RepeatStmt)
            and statement.runnable is not None,
        )
    tone: ProgressTone = (
        "normal" if event.status == "succeeded" else _tone(event.status)
    )
    if event.status == "failed" and error:
        return (*flow_error_rows(error, tone=tone), ProgressRow(f"  {text}", tone))
    return (ProgressRow(f"• {text}", tone),)


def collection_terminal_rows(
    statement: FlowStmt,
    event: StepEnd,
    *,
    fallback_total: int | None = None,
    error: str = "",
) -> tuple[ProgressRow, ...]:
    """Project one collection Flow Step as a semantic result sentence."""

    if event.status == "failed":
        return flow_error_rows(error, tone=_tone(event.status))
    if event.status == "canceled":
        return (ProgressRow("• canceled", _tone(event.status)),)
    noted = event.noted if isinstance(event.noted, CollectionStepNoted) else None
    total = noted.total_items if noted is not None else fallback_total
    output = noted.output_items if noted is not None else None
    if total is None:
        return ()
    text = _collection_success_text(statement, total, output)
    return (ProgressRow(f"• {text}", "normal"),) if text else ()


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
    statement: FlowStmt,
    error: str,
    observed_iterations: int = 0,
) -> tuple[str, ...]:
    """Project Flow-owned terminal content without synthesizing leaf activity."""

    if event.kind == "par" and event.status == "succeeded":
        rows: tuple[ProgressRow, ...] = ()
    elif event.kind == "loop":
        rows = loop_terminal_rows(
            event,
            statement=statement,
            observed_iterations=observed_iterations,
            error=error,
        )
    elif isinstance(statement, MapStmt | StormStmt | KeepStmt | DropStmt | RankStmt):
        rows = collection_terminal_rows(statement, event, error=error)
    else:
        rows = flow_terminal_rows(event, error=error)
    return tuple(
        row.text[2:] if index and row.text.startswith("  ") else row.text
        for index, row in enumerate(rows)
        if row.text
    )


def flow_error_rows(
    error: str,
    *,
    tone: ProgressTone = "error",
) -> tuple[ProgressRow, ...]:
    """Project one complete Flow- or Run-owned error into semantic rows."""

    return _error_rows("", error, tone) if error else ()


def lane_run_error_lines(error: str) -> tuple[str, ...]:
    """Project a complete Run-owned failure inside one parallel lane."""

    rows = _error_rows("failed", error, "error")
    return tuple(
        row.text[2:] if index and row.text.startswith("  ") else row.text
        for index, row in enumerate(rows)
    )


def _model_output_lines(event: StepEnd, *, include_text: bool = True) -> list[str]:
    lines: list[str] = []
    for part in output_parts(event):
        if isinstance(part, TextPart):
            if include_text:
                lines.extend(_split_lines(part.text))
        elif isinstance(part, ToolCallPart):
            name = part.tool_name or part.tool_family or "tool"
            lines.append(f"requested {name}")
        else:
            lines.extend(_json_lines(part.to_data()))
    if lines:
        return lines
    return _flow_output_lines(event) if include_text else []


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
            lines.append(_compact_json(output))
    return lines


def _flow_output_lines(event: StepEnd) -> list[str]:
    if event.output is None:
        return []
    try:
        parts = parts_from_local(event.output)
    except (TypeError, ValueError):
        return []
    lines: list[str] = []
    for part in parts:
        if isinstance(part, TextPart):
            lines.extend(_split_lines(part.text))
        else:
            lines.extend(_json_lines(part.to_data()))
    return lines


def _marked_rows(lines: list[str], tone: ProgressTone) -> tuple[ProgressRow, ...]:
    if not lines:
        return ()
    return tuple(
        ProgressRow(f"• {line}" if index == 0 else f"  {line}", tone)
        for index, line in enumerate(lines)
    )


def _error_rows(
    label: str,
    error: str,
    tone: ProgressTone,
) -> tuple[ProgressRow, ...]:
    lines = _split_lines(error.strip()) if error.strip() else []
    head = f"• {label}" if label else "•"
    if lines:
        head = f"{head} {lines[0]}"
    rows = [ProgressRow(head, tone)]
    rows.extend(ProgressRow(f"  {line}", tone) for line in lines[1:])
    return tuple(rows)


def _split_lines(value: str) -> list[str]:
    return value.splitlines() or ([value] if value else [])


def _json_lines(value: object) -> list[str]:
    try:
        return json.dumps(value, ensure_ascii=False, indent=2).splitlines()
    except TypeError:
        return _split_lines(str(value))


def _compact_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
        )
    except TypeError:
        return one_line(str(value))


def _count(value: int, noun: str) -> str:
    return f"{value} {noun}{'' if value == 1 else 's'}"


def _collection_success_text(
    statement: FlowStmt,
    total: int,
    output: int | None,
) -> str:
    if isinstance(statement, MapStmt):
        return f"Mapped {_all_items(total)} in parallel"
    if isinstance(statement, StormStmt):
        return f"Brainstormed {_count(output if output is not None else total, 'item')} in parallel"
    if isinstance(statement, KeepStmt):
        kept = output if output is not None else total
        if statement.position is not None:
            return _positional_keep_text(statement.position, total, kept)
        return (
            f"Evaluated {_count(total, 'item')} in parallel, "
            f"kept {_selected_items(kept, total)}"
        )
    if isinstance(statement, DropStmt):
        remaining = output if output is not None else total
        dropped = total - remaining
        if statement.position is not None:
            return _positional_drop_text(
                statement.position,
                total,
                dropped,
                remaining,
            )
        return (
            f"Evaluated {_count(total, 'item')} in parallel, "
            f"dropped {_selected_items(dropped, total)}, "
            f"leaving {_remaining_items(remaining, total)}"
        )
    if isinstance(statement, RankStmt):
        selected = output if output is not None else total
        lead = f"Scored {_count(total, 'item')} in parallel"
        if selected < total and statement.selection is not None:
            return f"{lead}, kept the {statement.selection} {selected}"
        return f"{lead}, ranked {_selected_items(selected, total)}"
    return ""


def _positional_keep_text(position: str, total: int, kept: int) -> str:
    if kept == 0:
        return "Kept no items"
    if kept == total:
        return f"Kept {_all_items(total)}"
    quantity = "item" if kept == 1 else f"{kept} items"
    return f"Kept the {position} {quantity} out of {total}"


def _positional_drop_text(
    position: str,
    total: int,
    dropped: int,
    remaining: int,
) -> str:
    if dropped == 0:
        return "Dropped no items"
    if dropped == total:
        return f"Dropped {_all_items(total)}, leaving none"
    quantity = "item" if dropped == 1 else f"{dropped} items"
    return f"Dropped the {position} {quantity} out of {total}, leaving {remaining}"


def _repeat_terminal_text(
    iterations: int,
    total: int | None,
    termination: str,
    *,
    has_condition: bool,
) -> str:
    if termination == "exhausted":
        completed = _completed_iterations(iterations)
        return (
            f"{completed} without meeting the condition" if has_condition else completed
        )
    if termination == "satisfied":
        return f"Condition met after {_iteration_progress(iterations, total)}"
    if iterations == 0:
        action = "Interrupted" if termination == "failed" else "Canceled"
        return f"{action} before completing an iteration"
    action = "Interrupted" if termination == "failed" else "Canceled"
    return f"{action} after completing {_iteration_progress(iterations, total)}"


def _settle_terminal_text(
    iterations: int,
    total: int | None,
    termination: str,
) -> str:
    if termination == "exhausted":
        if iterations == 0:
            return "Settled no items"
        return f"Settled {_all_items(iterations)} in {_count(iterations, 'iteration')}"
    action = "interrupted" if termination == "failed" else "canceled"
    if iterations == 0:
        return f"Settling was {action} before completing an iteration"
    return f"Settling was {action} after {_iteration_progress(iterations, total)}"


def _iteration_progress(iterations: int, total: int | None) -> str:
    if total is not None:
        return f"{iterations} of {_count(total, 'iteration')}"
    return _count(iterations, "iteration")


def _completed_iterations(iterations: int) -> str:
    if iterations == 0:
        return "Completed no iterations"
    if iterations == 1:
        return "Completed 1 iteration"
    return f"Completed all {iterations} iterations"


def _all_items(value: int) -> str:
    if value == 0:
        return "no items"
    if value == 1:
        return "the item"
    return f"all {value} items"


def _selected_items(value: int, total: int) -> str:
    if value == 0:
        return "none"
    if value == total:
        return f"all {total}"
    return str(value)


def _remaining_items(value: int, total: int) -> str:
    if value == 0:
        return "none"
    if value == total:
        return f"all {total}"
    return str(value)


def _tone(status: str) -> ProgressTone:
    if status == "failed":
        return "error"
    if status == "canceled":
        return "warning"
    return "progress"
