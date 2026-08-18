"""Stateful presentation blocks for ordered execution events."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from toolang.base.types.message import Part
from toolang.execution.events import RunEnd, StepEnd

from ..execution_progress.state import CallState, RunState, StatementState

from .console import ProgressConsole, Tone
from ..execution_progress.formatting import (
    count,
    elapsed,
    integer,
    model_label,
    one_line,
    output_preview,
    part_lines,
    shape_label,
    statement_head,
    statement_index,
    statement_result,
    statement_result_level,
    statement_target,
    status_label,
    text,
    tool_exit_code,
    tool_label,
    tool_result,
    truncate,
    usage_facts,
    value_summary,
)


@dataclass(slots=True)
class RunBlock(RunState):
    """One root or recursive run block."""

    indent: int = 0

    def render_header(
        self,
        console: ProgressConsole,
        *,
        verbosity: int,
        kind: str,
        name: str,
        doc: str,
        input_value: tuple[Part, ...],
        args: Mapping[str, object],
        control_index: int,
    ) -> None:
        label = " ".join(
            value for value in (kind or self.kind, name or self.name) if value
        )
        console.write(f"Run {label}")
        if verbosity >= 1 and doc:
            console.wrapped(doc, prefix="")
        console.blank()
        if verbosity < 2:
            return
        input_lines = part_lines(input_value)
        if input_lines:
            console.wrapped(input_lines[0], prefix="> ", continuation="  ")
            for line in input_lines[1:]:
                console.wrapped(line, prefix="  ")
        for arg_name, value in args.items():
            console.write(f"  {arg_name}={value_summary(value)}")
        console.write(f"  {self.run_id}@{control_index}")
        console.blank()

    def render_result(
        self,
        console: ProgressConsole,
        event: RunEnd,
        *,
        output: StepEnd | None,
        error: str,
    ) -> None:
        console.clear_live()
        console.blank()
        status = status_label(event.status)
        tone = _tone(event.status)
        title = f"--- {event.run} {status} ---"
        console.write(title, tone=tone)
        if event.status == "succeeded" and output is not None:
            shape = shape_label(output)
            if shape:
                console.write(f"{shape} returned")
        elif error:
            console.wrapped(error, prefix="", tone=tone)
        duration = elapsed(self.started_at, event.finished_at)
        facts = self.metrics.facts(
            duration=duration,
            include_runs=False,
        )
        if self.metrics.runs > 1:
            facts.insert(1 if duration else 0, count(self.metrics.runs - 1, "run"))
        if facts:
            console.wrapped(" · ".join(facts), prefix="")
        console.write("-" * len(title), tone=tone)

    def render_compact(
        self,
        console: ProgressConsole,
        *,
        finished_at: str,
    ) -> None:
        """Close one visible child run without a nested frame."""

        facts = [
            f"{self.run_id} {status_label(self.status)}",
            elapsed(self.started_at, finished_at),
        ]
        console.wrapped(
            " · ".join(value for value in facts if value),
            prefix=f"{' ' * self.indent}↳ ",
            continuation=f"{' ' * (self.indent + 2)}",
            tone=_tone(self.status),
        )


@dataclass(slots=True)
class CallBlock(CallState):
    """One model, tool, or system step within an agic."""

    def render_begin(
        self,
        console: ProgressConsole,
        *,
        indent: int,
        batched: bool,
    ) -> None:
        if console.tty and not batched:
            console.show_live([f"{' ' * indent}· {self.active_label}"])

    def render_delta(
        self,
        console: ProgressConsole,
        delta: str,
        *,
        indent: int,
        batched: bool,
    ) -> str:
        self.append_delta(delta)
        value = truncate(one_line(self.preview), 100)
        if console.tty and not batched and value:
            console.show_live([f"{' ' * indent}· {value}"])
        return value

    def render_end(
        self,
        console: ProgressConsole,
        event: StepEnd,
        *,
        verbosity: int,
        indent: int,
        batched: bool,
        error: str,
    ) -> None:
        console.clear_live()
        if batched and event.status == "succeeded":
            return
        if self.begin.kind == "model":
            self._render_model(
                console,
                event,
                verbosity=verbosity,
                indent=indent,
                error=error,
            )
        elif self.begin.kind == "tool":
            self._render_tool(
                console,
                event,
                verbosity=verbosity,
                indent=indent,
                error=error,
            )
        elif event.status != "succeeded" and error:
            tone = _tone(event.status)
            console.wrapped(
                f"{event.step} {status_label(event.status)}: {error}",
                prefix=f"{' ' * indent}! ",
                continuation=f"{' ' * (indent + 2)}",
                tone=tone,
            )

    def _render_model(
        self,
        console: ProgressConsole,
        event: StepEnd,
        *,
        verbosity: int,
        indent: int,
        error: str,
    ) -> None:
        model = model_label(self.begin.given)
        duration = elapsed(self.begin.started_at, event.finished_at)
        facts = [str(event.step), duration, model, *usage_facts(event.noted)]
        if event.status != "succeeded":
            if not error:
                return
            tone = _tone(event.status)
            console.wrapped(
                f"{model}: {error}",
                prefix=f"{' ' * indent}! ",
                continuation=f"{' ' * (indent + 2)}",
                tone=tone,
            )
            console.wrapped(
                " · ".join(fact for fact in facts if fact),
                prefix=f"{' ' * (indent + 2)}",
            )
            return
        console.wrapped(
            output_preview(event) or "model completed",
            prefix=f"{' ' * indent}· ",
            continuation=f"{' ' * (indent + 2)}",
        )
        if verbosity >= 2:
            console.wrapped(
                " · ".join(fact for fact in facts if fact),
                prefix=f"{' ' * (indent + 2)}",
            )

    def _render_tool(
        self,
        console: ProgressConsole,
        event: StepEnd,
        *,
        verbosity: int,
        indent: int,
        error: str,
    ) -> None:
        tool = tool_label(self.begin.given)
        duration = elapsed(self.begin.started_at, event.finished_at)
        exit_code = tool_exit_code(event)
        facts = [
            str(event.step),
            duration,
            f"exit {exit_code}" if exit_code is not None else "",
        ]
        if event.status != "succeeded":
            if not error:
                return
            tone = _tone(event.status)
            console.wrapped(
                f"{tool}: {error}",
                prefix=f"{' ' * indent}! ",
                continuation=f"{' ' * (indent + 2)}",
                tone=tone,
            )
            console.wrapped(
                " · ".join(fact for fact in facts if fact),
                prefix=f"{' ' * (indent + 2)}",
            )
            return
        result = tool_result(event)
        console.wrapped(
            f"{tool}: {result or 'completed'}",
            prefix=f"{' ' * indent}· ",
            continuation=f"{' ' * (indent + 2)}",
        )
        if verbosity >= 2:
            console.wrapped(
                " · ".join(fact for fact in facts if fact),
                prefix=f"{' ' * (indent + 2)}",
            )


@dataclass(slots=True)
class StatementBlock(StatementState):
    """One authored flow statement and its child-work presentation."""

    base_indent: int = 0
    work_written: bool = False
    hidden: bool = False
    header_written: bool = False

    @property
    def content_indent(self) -> int:
        return self.base_indent + 2

    def render_begin(
        self,
        console: ProgressConsole,
        *,
        verbosity: int,
    ) -> None:
        if self.hidden:
            return
        self._render_header(console, verbosity=verbosity)

    def _render_header(
        self,
        console: ProgressConsole,
        *,
        verbosity: int,
    ) -> None:
        if self.header_written:
            return
        self.header_written = True
        console.blank()
        index = self.ordinal
        if index is None:
            index = statement_index(self.begin.step)
        heading = f"[{index}] {statement_head(self.begin.given)}"
        console.write(f"{' ' * self.base_indent}{heading}")
        if verbosity >= 1 and (doc := text(self.begin.given.get("doc"))):
            console.wrapped(doc, prefix=" " * self.content_indent)
            console.blank()

    def enter_iteration(
        self,
        console: ProgressConsole,
        iteration: int,
        *,
        verbosity: int,
    ) -> int:
        """Enter one repeat iteration and allocate its local ordinal."""

        changed = self.current_iteration != iteration
        ordinal = self.note_iteration(iteration)
        if changed:
            if verbosity >= 2:
                console.blank()
                console.write(
                    f"{' ' * self.content_indent}=== iteration {iteration} ==="
                )
                console.blank()
        return ordinal

    def render_until_begin(
        self,
        console: ProgressConsole,
        run: RunBlock,
        *,
        verbosity: int,
    ) -> None:
        """Open the repeat until clause as a sibling presentation block."""

        self.begin_until(run)
        if verbosity < 2:
            return
        console.blank()
        console.write(f"{' ' * self.content_indent}[?] until")
        console.write(f"{' ' * (self.content_indent + 2)}Run {run.label}")

    def render_until_decision(
        self,
        console: ProgressConsole,
        decision: bool | None,
        *,
        verbosity: int,
    ) -> None:
        """Record a successful until decision without guessing invalid output."""

        self.record_until_decision(decision)
        if decision is None:
            return
        label = "stop repeating" if decision else "continue"
        if verbosity >= 2:
            console.blank()
            console.write(f"{' ' * (self.content_indent + 2)}↳ {label}")

    def render_work(
        self,
        console: ProgressConsole,
        run: RunBlock,
        *,
        verbosity: int,
    ) -> None:
        if self.work_written:
            return
        self.work_written = True
        if self.hidden:
            return
        console.write(f"{' ' * self.content_indent}{self.work_line(run)}")

    def live_lines(self, timestamp: str) -> list[str]:
        base = " " * self.content_indent
        duration = elapsed(self.begin.started_at, timestamp)
        if self.begin.kind == "par":
            progress = [
                f"{count(self.completed, 'run')} succeeded" if self.completed else "",
                f"{len(self.lanes)} active" if self.lanes else "",
                f"{self.failed} failed" if self.failed else "",
                duration,
            ]
            lines = [f"{base}· {' · '.join(value for value in progress if value)}"]
            width = len(str(max((self.lane_count or 1) - 1, 0)))
            for lane_index, lane in sorted(self.lanes.items()):
                lines.append(
                    f"{base}{lane_index:>{width}} │ item {lane.item} | {lane.activity}"
                )
            return lines
        if self.statement == "settle":
            active = 1 if self.active_run is not None else 0
            progress = [
                f"{count(self.completed, 'run')} succeeded",
                f"{active} active" if active else "",
                f"{self.failed} failed" if self.failed else "",
            ]
            if duration:
                progress.append(duration)
            lines = [f"{base}· {' · '.join(value for value in progress if value)}"]
            if self.active_run is not None and self.active_item is not None:
                lines.append(
                    f"{base}│ item {self.active_item} | {self.active_activity}"
                )
            return lines
        if self.statement == "repeat":
            iteration = self.current_iteration
            lines = [
                f"{base}=== iteration {iteration} ==="
                if iteration is not None
                else f"{base}=== starting ==="
            ]
            if self.active_title == "until":
                lines.append(f"{base}[?] until")
            elif self.active_ordinal is not None and self.active_title:
                lines.append(f"{base}[{self.active_ordinal}] {self.active_title}")
            nested = " " * (self.content_indent + 2)
            if self.active_work:
                lines.append(f"{nested}{self.active_work}")
            if self.active_activity:
                marker = "" if self.active_activity.startswith("↳ ") else "· "
                lines.append(f"{nested}{marker}{self.active_activity}")
            return lines
        return []

    def render_end(
        self,
        console: ProgressConsole,
        event: StepEnd,
        *,
        verbosity: int,
        error: str,
    ) -> None:
        console.clear_live()
        if self.hidden:
            return
        if not self.header_written:
            self._render_header(console, verbosity=verbosity)
        if event.status != "succeeded":
            if error:
                tone = _tone(event.status)
                detail = (
                    f"item {self.failed_item}: {error}"
                    if self.failed_item is not None
                    else error
                )
                console.wrapped(
                    f"{event.step} {status_label(event.status)}: {detail}",
                    prefix=f"{' ' * self.content_indent}! ",
                    continuation=f"{' ' * (self.content_indent + 2)}",
                    tone=tone,
                )
                facts = [
                    f"{count(self.completed, 'run')} succeeded"
                    if self.completed
                    else "",
                    f"{self.failed} failed" if self.failed else "",
                    elapsed(self.begin.started_at, event.finished_at),
                ]
                if fact_text := " · ".join(fact for fact in facts if fact):
                    console.wrapped(
                        fact_text,
                        prefix=f"{' ' * self.content_indent}· ",
                        continuation=f"{' ' * (self.content_indent + 2)}",
                    )
            return
        if not self.work_written and statement_target(self.begin.given):
            self.work_written = True
            console.write(f"{' ' * self.content_indent}{self.work_line()}")
        if self.statement == "repeat":
            completed_iterations = (
                self.current_iteration + 1 if self.current_iteration is not None else 0
            )
            limit = integer(self.begin.given.get("count"))
            stopped_early = self.until_decision is True and (
                limit is None or completed_iterations < limit
            )
            if stopped_early:
                console.blank()
                console.write(
                    f"{' ' * self.content_indent}↳ stopped after "
                    f"{count(completed_iterations, 'iteration')}"
                )
            return
        aggregate_written = (
            verbosity >= 1
            and self.begin.kind in {"par", "loop"}
            and self.statement != "repeat"
        )
        if aggregate_written:
            self._render_parallel_result(console, event)
        level = statement_result_level(self.begin.given)
        if level is None or verbosity < level:
            return
        source_items = (
            self.total
            if self.total is not None
            else len(self.children)
            if self.begin.kind in {"par", "loop"}
            else None
        )
        result = statement_result(
            self.begin.given,
            event,
            source_items=source_items,
        )
        if not result:
            return
        console.wrapped(
            result,
            prefix=f"{' ' * self.content_indent}↳ ",
            continuation=f"{' ' * (self.content_indent + 2)}",
        )

    def _render_parallel_result(
        self,
        console: ProgressConsole,
        event: StepEnd,
    ) -> None:
        direct_runs = len(self.children)
        if direct_runs:
            facts = [
                f"{count(self.completed, 'run')} succeeded",
                f"{self.failed} failed" if self.failed else "",
            ]
            facts.extend(
                self.metrics.facts(
                    duration=elapsed(self.begin.started_at, event.finished_at),
                    include_runs=False,
                )
            )
        else:
            facts = ["0 runs", "empty input list"]
        console.wrapped(
            " · ".join(fact for fact in facts if fact),
            prefix=f"{' ' * self.content_indent}· ",
            continuation=f"{' ' * (self.content_indent + 2)}",
        )


def _tone(status: str) -> Tone:
    if status == "failed":
        return "error"
    if status == "canceled":
        return "warning"
    return "progress"
