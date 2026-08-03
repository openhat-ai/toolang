"""Mutable blocks for terminal chat."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from rich import box
from rich.console import Group, RenderableType
from rich.markdown import Markdown
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from toolang.base.types.message import (
    MessagePart,
    TextDelta,
    TextPart,
    ToolCallPart,
    ToolResultPart,
)
from toolang.execution.events import (
    PartDelta,
    RunBegin,
    RunEnd,
    RunEvent,
    StepBegin,
    StepEnd,
)
from toolang.execution.types import StepPath

from toolang.cli.common.execution_progress.formatting import (
    count,
    elapsed,
    model_label,
    statement_head,
    statement_index,
    statement_result,
    status_label,
    shape_label,
    tool_exit_code,
    tool_label,
    tool_result,
    usage_facts,
)
from toolang.cli.common.execution_progress.state import (
    CallState,
    Metrics,
    RunState,
    StatementState,
)

from .base import as_text, friendly_error
from .rendering import (
    bar,
    display_len,
    markdown_width,
    progress_tail,
    render_segments,
    summarize,
    terminal_width,
    truncate_display,
)

STEER_BAR_BG = "#2f555d"


def _terminal_diagnostic(status: str, error: str) -> str:
    value = friendly_error(error) if error else ""
    normalized = re.sub(r"[\s._-]+", " ", value.casefold()).strip(" .:!")
    if status == "canceled" and normalized in {
        "canceled",
        "cancelled",
        "run canceled",
        "run cancelled",
        "operation canceled",
        "operation cancelled",
        "interrupted by user",
    }:
        return ""
    return value


class MutableBlock:
    """A live UI block that the TUI can later move into scrollback."""

    @property
    def type(self) -> str:
        return self.__class__.__name__

    def update(self, event: Any) -> None:
        raise NotImplementedError

    def render(self) -> RenderableType | None:
        raise NotImplementedError


@dataclass(slots=True)
class RunStartBlock(MutableBlock):
    """Created by a local submission and finalized by run_begin/run_end."""

    message: str
    run_id: str = ""

    @classmethod
    def create(cls, message: str) -> "RunStartBlock":
        return cls(message=message)

    def update(self, event: RunEvent) -> None:
        if isinstance(event, (RunBegin, RunEnd)):
            self.run_id = event.run or self.run_id

    def render(self) -> RenderableType:
        lines: list[RenderableType] = [bar([], style="white on grey23")]
        for index, line in enumerate(self.message.splitlines() or [""]):
            lines.append(
                bar([(">", "grey70 on grey23"), (f" {line}", "white on grey23")])
                if index == 0
                else bar([(f"  {line}", "white on grey23")], style="white on grey23")
            )
        if self.run_id:
            lines.append(
                bar(
                    [(f"  {self.run_id}", "grey70 on grey23")],
                    style="white on grey23",
                )
            )
        lines.append(Text("\n"))
        return Group(*lines)


@dataclass(slots=True)
class RunSteerBlock(MutableBlock):
    """Created by a local steer and moved by the next step_begin or run_end."""

    message: str
    run_id: str = ""
    pending: bool = True

    @classmethod
    def create(cls, *, message: str, run_id: str) -> "RunSteerBlock":
        return cls(message=message, run_id=run_id)

    def update(self, event: StepBegin | RunEnd) -> None:
        del event
        self.pending = False

    def render(self) -> RenderableType:
        footer = "  pending for next step" if self.pending else ""
        bg = STEER_BAR_BG
        lines: list[RenderableType] = [Text(), bar([], style=f"white on {bg}")]
        for index, line in enumerate(self.message.splitlines() or [""]):
            lines.append(
                bar(
                    [("+", f"grey70 on {bg}"), (f" {line}", f"white on {bg}")],
                    style=f"white on {bg}",
                )
                if index == 0
                else bar([(f"  {line}", f"white on {bg}")], style=f"white on {bg}")
            )
        lines.extend(
            [bar([(footer, f"grey70 on {bg}")], style=f"white on {bg}"), Text("\n")]
        )
        return Group(*lines)


@dataclass(slots=True)
class RunStopBlock(MutableBlock):
    """Created by run_begin and finalized by run_end."""

    run_id: str
    status: str
    error: str = ""
    started_at: str = ""
    finished_at: str = ""
    metrics: Metrics = field(default_factory=Metrics)
    include_child_runs: bool = False

    @classmethod
    def create(cls, event: RunBegin | RunEnd) -> "RunStopBlock":
        if isinstance(event, RunBegin):
            return cls(
                run_id=event.run or "run",
                status="running",
                started_at=event.started_at,
            )
        run_end = event
        return cls(
            run_id=run_end.run or "run",
            status=run_end.status,
            error=friendly_error(run_end.error) if run_end.error else "",
            finished_at=run_end.finished_at,
        )

    def update(self, event: RunBegin | RunEnd) -> None:
        self.run_id = event.run or self.run_id
        if isinstance(event, RunBegin):
            self.status = "running"
            self.error = ""
            return
        run_end = event
        self.status = run_end.status
        self.error = friendly_error(run_end.error) if run_end.error else self.error
        self.finished_at = run_end.finished_at

    def set_metrics(
        self,
        metrics: Metrics,
        *,
        include_child_runs: bool = False,
    ) -> None:
        self.metrics = metrics
        self.include_child_runs = include_child_runs

    def mark_canceling(self) -> None:
        self.status = "canceling"
        self.error = ""

    def render(self) -> RenderableType:
        run_id = self.run_id
        status = self.status

        if status == "running":
            return Text("\n")

        if status == "canceling":
            return Text.from_markup("[dim]canceling...[/]")

        label = status_label(status)
        tone = (
            "green"
            if status == "finished"
            else "red"
            if status == "failed"
            else "yellow"
            if status == "canceled"
            else "dim"
        )
        lines: list[RenderableType] = []
        message = _terminal_diagnostic(status, self.error)
        if message:
            lines.extend(
                Text.from_markup(f"[{tone}]! {escape(line)}[/]")
                for line in self._wrap_plain_lines(message)
            )
        facts = self._facts()
        suffix = f" · {' · '.join(facts)}" if facts else ""
        summary = Text()
        summary.append("◆ ", style=tone)
        summary.append(f"{run_id} ", style="dim")
        summary.append(label, style=tone)
        summary.append(suffix, style="dim")
        lines.extend([Text(), summary, Text("\n")])
        return Group(*lines)

    def _facts(self) -> list[str]:
        duration = elapsed(self.started_at, self.finished_at)
        facts = self.metrics.facts(duration=duration, include_runs=False)
        if self.include_child_runs:
            facts.insert(
                1 if duration else 0,
                count(max(self.metrics.runs - 1, 0), "run"),
            )
        return facts

    @staticmethod
    def _wrap_plain_lines(text: str) -> list[str]:
        width = max(terminal_width() - 2, 20)
        lines: list[str] = []
        for raw_line in text.splitlines() or [""]:
            line = raw_line.strip()
            while len(line) > width:
                split_at = line.rfind(" ", 0, width + 1)
                split_at = width if split_at <= 0 else split_at
                lines.append(line[:split_at].rstrip())
                line = line[split_at:].lstrip()
            if line:
                lines.append(line)
        return lines


@dataclass(slots=True)
class DefaultStepBlock(MutableBlock):
    """Fallback step block for step kinds that do not have a dedicated block yet."""

    step: StepPath
    step_kind: str
    label: str = ""
    status: str = "running"
    final_label: str = ""
    error: str = ""

    @classmethod
    def create(cls, event: StepBegin) -> "DefaultStepBlock":
        step_kind = event.kind
        payload = event.given
        return cls(
            step=event.step,
            step_kind=step_kind,
            label=cls._initial_label(step_kind, payload),
        )

    @staticmethod
    def _initial_label(step_kind: str, payload: Mapping[str, Any]) -> str:
        if step_kind == "system":
            return (
                as_text(payload.get("message"))
                or as_text(payload.get("statement"))
                or step_kind
            )
        return f"running {step_kind}"

    def update(self, event: StepEnd) -> None:
        self.step = event.step
        self.status = "completed"
        self.error = event.error or ""
        self.final_label = self._final_label(event.noted)

    def render(self) -> RenderableType:
        kind = self.step_kind
        marker = self._marker()
        running = self.status != "completed"

        if running:
            line = progress_tail(f"{marker} {self.label}")
            return Text.from_markup(f"[none]{escape(line)}[/]")

        if kind == "system":
            message = self.error or self.final_label or self.label or "runtime event"
            tone = "red" if self.error else "dim"
            prefix = "!" if self.error else marker
            return Text.from_markup(f"[{tone}]{escape(f'{prefix} {message}')}[/]")

        label = kind or "step"
        if self.error:
            return Text.from_markup(
                f"[red]! {escape(f'{self.step} failed: {self.error}')}[/]"
            )
        return Text.from_markup(f"[dim]{escape(f'{marker} {label} completed')}[/]")

    def _marker(self) -> str:
        return "·"

    def _final_label(self, payload: Mapping[str, Any]) -> str:
        return (
            as_text(payload.get("message"))
            or as_text(payload.get("statement"))
            or self.label.removeprefix("running ")
        )


@dataclass(slots=True)
class FlowStepBlock(MutableBlock):
    """Flow operation step block."""

    state: StatementState
    display_error: str = ""
    calls: list[CallState] = field(default_factory=list)
    child_runs: list[RunState] = field(default_factory=list)

    @property
    def step(self) -> StepPath:
        return self.state.begin.step

    @classmethod
    def create(cls, event: StepBegin) -> "FlowStepBlock":
        return cls(state=StatementState(event))

    @classmethod
    def from_state(cls, state: StatementState) -> "FlowStepBlock":
        return cls(state=state)

    def update(self, event: StepEnd) -> None:
        self.state.finish(event)
        self.display_error = event.error or ""

    def note_call(self, call: CallState) -> None:
        """Retain one direct child call as detailed statement progress."""

        self.calls.append(call)

    def note_child_run(self, run: RunState) -> None:
        """Retain one direct child boundary for the detailed transcript."""

        self.child_runs.append(run)

    def render(self) -> RenderableType:
        state = self.state
        end = state.end
        ordinal = state.ordinal
        if ordinal is None:
            ordinal = statement_index(state.begin.step)
        lines: list[RenderableType] = [
            Text.from_markup(
                f"[dim]{escape(f'[{ordinal}] {statement_head(state.begin.given)}')}[/]"
            )
        ]
        has_doc = False
        if doc := as_text(state.begin.given.get("doc")):
            has_doc = True
            lines.extend(
                Text.from_markup(f"[dim]  {escape(line)}[/]")
                for line in doc.splitlines()
            )
        work = state.active_work
        if not work and end is not None and self._has_runnable_target():
            work = state.work_line()
        if work:
            if has_doc:
                lines.append(Text())
            lines.append(Text.from_markup(f"[dim]  {escape(work)}[/]"))
        if end is None:
            lines.extend(self._live_lines())
            return Group(*lines)
        lines.extend(self._call_lines())
        lines.extend(self._child_run_lines())
        if end.status != "finished":
            error = _terminal_diagnostic(
                end.status,
                self.display_error or end.error or "",
            )
            if not error and end.status == "failed":
                error = "statement failed"
            detail = f"{end.step} {status_label(end.status)}"
            if error:
                error = (
                    f"item {state.failed_item}: {error}"
                    if state.failed_item is not None
                    else error
                )
                detail = f"{detail}: {error}"
            tone = "yellow" if end.status == "canceled" else "red"
            lines.append(Text.from_markup(f"[{tone}]  ! {escape(detail)}[/]"))
            facts = self._aggregate_facts(end)
            if facts:
                lines.append(Text.from_markup(f"[dim]    {escape(facts)}[/]"))
            return Group(*lines, Text("\n"))
        if state.statement == "repeat":
            iterations = (
                state.current_iteration + 1
                if state.current_iteration is not None
                else 0
            )
            facts = [
                count(iterations, "iteration"),
                elapsed(state.begin.started_at, end.finished_at),
            ]
            lines.append(
                Text.from_markup(
                    f"[dim]  · {escape(' · '.join(fact for fact in facts if fact))}[/]"
                )
            )
            if state.until_decision is True:
                lines.append(
                    Text.from_markup(
                        f"[dim]  ↳ stopped after {escape(count(iterations, 'iteration'))}[/]"
                    )
                )
            return Group(*lines, Text("\n"))
        aggregate = self._aggregate_facts(end)
        if aggregate:
            lines.append(Text.from_markup(f"[dim]  · {escape(aggregate)}[/]"))
        source_items = state.total if state.total is not None else len(state.children)
        result = statement_result(
            state.begin.given,
            end,
            source_items=source_items if state.batched else None,
        )
        if result:
            lines.append(Text.from_markup(f"[dim]  ↳ {escape(result)}[/]"))
        return Group(*lines, Text("\n"))

    def _live_lines(self) -> list[RenderableType]:
        state = self.state
        if state.begin.kind == "par":
            facts = [
                f"{count(state.completed, 'run')} succeeded" if state.completed else "",
                f"{len(state.lanes)} active" if state.lanes else "",
                f"{state.failed} failed" if state.failed else "",
            ]
            rows: list[RenderableType] = [
                Text.from_markup(
                    f"[none]  · {escape(' · '.join(fact for fact in facts if fact) or 'starting…')}[/]"
                )
            ]
            width = len(str(max((state.lane_count or 1) - 1, 0)))
            for lane, item in sorted(state.lanes.items()):
                value = f"  {lane:>{width}} │ item {item.item} | {item.activity}"
                rows.append(Text.from_markup(f"[none]{escape(value)}[/]"))
            return rows
        if state.statement == "repeat":
            iteration = state.current_iteration
            rows: list[RenderableType] = [
                Text.from_markup(
                    f"[dim]  === {escape(f'iteration {iteration}' if iteration is not None else 'starting')} ===[/]"
                )
            ]
            if state.active_title:
                marker = "?" if state.active_title == "until" else state.active_ordinal
                rows.append(
                    Text.from_markup(
                        f"[dim]  [{marker}] {escape(state.active_title)}[/]"
                    )
                )
            if state.active_activity:
                prefix = "" if state.active_activity.startswith("↳ ") else "· "
                rows.append(
                    Text.from_markup(
                        f"[none]  {prefix}{escape(state.active_activity)}[/]"
                    )
                )
            return rows
        activity = state.active_activity or "running…"
        prefix = "" if activity.startswith("↳ ") else "· "
        return [Text.from_markup(f"[none]  {prefix}{escape(activity)}[/]")]

    def _call_lines(self) -> list[RenderableType]:
        lines: list[RenderableType] = []
        for call in self.calls:
            end = call.end
            if end is None or end.status != "finished":
                continue
            begin = call.begin
            if begin.kind == "model":
                label = "model completed"
                facts = [
                    end.step,
                    elapsed(begin.started_at, end.finished_at),
                    model_label(begin.given),
                    *usage_facts(end.noted),
                ]
            elif begin.kind == "tool":
                tool = tool_label(begin.given)
                label = f"{tool}: {tool_result(end) or 'completed'}"
                code = tool_exit_code(end)
                facts = [
                    end.step,
                    elapsed(begin.started_at, end.finished_at),
                    f"exit {code}" if code is not None else "",
                ]
            else:
                label = f"{begin.kind} completed"
                facts = [end.step, elapsed(begin.started_at, end.finished_at)]
            lines.append(Text.from_markup(f"[dim]  · {escape(label)}[/]"))
            fact_text = " · ".join(fact for fact in facts if fact)
            if fact_text:
                lines.append(Text.from_markup(f"[dim]    {escape(fact_text)}[/]"))
        return lines

    def _child_run_lines(self) -> list[RenderableType]:
        lines: list[RenderableType] = []
        for run in self.child_runs:
            facts = [
                f"{run.run_id} {status_label(run.status)}",
                elapsed(run.started_at, run.finished_at),
            ]
            tone = (
                "red"
                if run.status == "failed"
                else "yellow"
                if run.status == "canceled"
                else "dim"
            )
            lines.append(
                Text.from_markup(
                    f"[{tone}]  ↳ {escape(' · '.join(fact for fact in facts if fact))}[/]"
                )
            )
        return lines

    def _has_runnable_target(self) -> bool:
        given = self.state.begin.given
        return any(
            as_text(given.get(key))
            for key in ("runnable", "predicate", "scorer", "agent")
        )

    def _aggregate_facts(self, end: StepEnd) -> str:
        state = self.state
        if not state.batched:
            return ""
        if not state.children:
            return "0 runs · empty input list"
        facts = [
            f"{count(state.completed, 'run')} succeeded",
            f"{state.failed} failed" if state.failed else "",
            *state.metrics.facts(
                duration=elapsed(state.begin.started_at, end.finished_at),
                include_runs=False,
            ),
        ]
        return " · ".join(fact for fact in facts if fact)


@dataclass(slots=True)
class ModelStepBlock(MutableBlock):
    """Model step block."""

    step: StepPath
    status: str = "thinking"
    message: str = ""
    output: str = ""
    tool_requests: list[str] = field(default_factory=list)
    model: str = ""
    error: str = ""
    started_at: str = ""
    finished_at: str = ""
    noted: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def create(cls, event: StepBegin) -> "ModelStepBlock":
        payload = event.given
        model = payload.get("model")
        model_data = model if isinstance(model, Mapping) else {}
        return cls(
            step=event.step,
            model=as_text(model_data.get("ref"))
            or as_text(model_data.get("model"))
            or "",
            started_at=event.started_at,
        )

    def update(self, event: PartDelta | StepEnd) -> None:
        self.step = event.step
        if isinstance(event, PartDelta):
            delta = event.delta
            if isinstance(delta, TextDelta):
                self.message += delta.text
        else:
            self.status = event.status
            self.output = _parts_text(event.output)
            self.tool_requests = self._tool_request_summary(event)
            self.error = event.error or ""
            self.finished_at = event.finished_at
            self.noted = event.noted

    def hide_internal_output(self) -> None:
        """Keep call facts while removing an unconfirmed assistant response."""

        self.message = ""
        self.output = ""

    def render(self) -> RenderableType:
        running = self.status == "thinking"
        message = self.message
        output = self.output.strip()
        requests = "; ".join(self.tool_requests)

        if running:
            if message:
                return self._render_markdown_output(
                    [progress_tail(" ".join(message.split()))]
                )
            return Text.from_markup("[none]· thinking…[/]")

        if self.status != "finished":
            label = self.model or "model"
            error = _terminal_diagnostic(self.status, self.error)
            detail = (
                "model call canceled"
                if self.status == "canceled" and not error
                else f"{label} {status_label(self.status)}"
            )
            if error:
                detail = f"{label}: {error}"
            tone = "yellow" if self.status == "canceled" else "red"
            return self._with_facts(Text.from_markup(f"[{tone}]! {escape(detail)}[/]"))

        if output:
            output_lines = output.splitlines() or [output]
            return self._with_facts(self._render_markdown_output(output_lines))

        if requests:
            return self._with_facts(
                Text.from_markup(f"[dim]· {escape(f'requested {requests}')}[/]")
            )

        suffix = f" ({self.model})" if self.model else ""
        return self._with_facts(
            Text.from_markup(f"[dim]· {escape(f'model completed{suffix}')}[/]")
        )

    def _with_facts(self, output: RenderableType) -> RenderableType:
        facts = [
            self.step,
            elapsed(self.started_at, self.finished_at),
            self.model or "model",
            *usage_facts(self.noted),
        ]
        fact_text = " · ".join(fact for fact in facts if fact)
        if not fact_text:
            return output
        return Group(output, Text.from_markup(f"[dim]  {escape(fact_text)}[/]"))

    @staticmethod
    def _render_markdown_output(lines: Sequence[str]) -> RenderableType:
        rows: list[list[tuple[str, Any]]] = [[]]
        width = max(20, markdown_width() - 2)
        for segment in render_segments(Markdown("\n".join(lines)), width=width):
            if segment.control or not segment.text:
                continue
            parts = segment.text.split("\n")
            for index, part in enumerate(parts):
                if index:
                    rows.append([])
                if part:
                    rows[-1].append((part, segment.style))

        while rows and not any(text.strip() for text, _style in rows[0]):
            rows.pop(0)
        while rows and not any(text.strip() for text, _style in rows[-1]):
            rows.pop()
        if not rows:
            return Text.from_markup("[none]·[/]")

        rendered_rows: list[Text] = []
        for index, row in enumerate(rows):
            line = Text()
            if index == 0:
                line.append("·")
                line.append(" ")
            else:
                line.append("  ")
            for text, style in row:
                line.append(text, style=style)
            rendered_rows.append(line)
        return Group(*rendered_rows)

    @staticmethod
    def _tool_request_summary(event: StepEnd) -> list[str]:
        tools: list[str] = []
        for part in event.output:
            if not isinstance(part, ToolCallPart):
                continue
            tools.append(
                _tool_call_display(
                    part.tool_name or part.tool_family or "tool", dict(part.input)
                )
            )
        return tools


@dataclass(frozen=True, slots=True)
class AssistantResponseBlock(MutableBlock):
    """One root output confirmed as stable conversation content."""

    text: str
    shape: str = ""

    @classmethod
    def create(cls, event: StepEnd) -> "AssistantResponseBlock":
        return cls(text=_parts_text(event.output), shape=shape_label(event))

    @classmethod
    def from_parts(cls, parts: Sequence[MessagePart]) -> "AssistantResponseBlock":
        text = _parts_text(parts)
        if not text and parts:
            text = json.dumps(
                [part.to_data() for part in parts],
                ensure_ascii=False,
                indent=2,
            )
        return cls(text=text)

    def update(self, event: Any) -> None:
        del event

    def render(self) -> RenderableType | None:
        if self.text:
            return ModelStepBlock._render_markdown_output(self.text.splitlines())
        if self.shape:
            return Text.from_markup(f"[dim]· {escape(f'{self.shape} returned')}[/]")
        return None


@dataclass(frozen=True, slots=True)
class SlashResultBlock:
    """Render a structured slash-command result with a terminal boundary."""

    parts: Sequence[MessagePart]

    def render(self) -> RenderableType:
        response = AssistantResponseBlock.from_parts(self.parts).render()
        if response is None:
            return Text("\n")
        return Group(response, Text("\n"))


@dataclass(frozen=True, slots=True)
class ResultAvailableBlock(MutableBlock):
    """Tell the user how to reopen one durable flow result."""

    run_id: str

    def update(self, event: Any) -> None:
        del event

    def render(self) -> RenderableType:
        line = Text("◇ result saved · ")
        line.append(f":show {self.run_id}", style="cyan")
        return line


@dataclass(slots=True)
class ToolStepBlock(MutableBlock):
    """Tool step block."""

    step: StepPath
    detail: str
    status: str = "running"
    error: str = ""
    output_messages: list[str] = field(default_factory=list)
    started_at: str = ""
    finished_at: str = ""
    exit_code: int | None = None

    @classmethod
    def create(cls, event: StepBegin) -> "ToolStepBlock":
        return cls(
            step=event.step,
            detail=tool_label(event.given),
            started_at=event.started_at,
        )

    def update(self, event: StepEnd) -> None:
        self.step = event.step
        self.status = "completed"
        self.detail = _tool_call_display_from_parts(event.output)
        self.error = event.error or ""
        self.output_messages = self._output_messages(event)
        self.finished_at = event.finished_at
        self.exit_code = tool_exit_code(event)

    def render(self) -> RenderableType:
        detail = self.detail
        output = " ".join("\n".join(self.output_messages).split())

        if self.status == "running":
            return Text.from_markup(f"[none]{escape(f'· executing {detail}…')}[/]")

        if self.error:
            message = f"{detail}: {summarize(self.error, width=120)}"
            line = Text.from_markup(f"[red]! {escape(message)}[/]")
            if output:
                width = max(8, markdown_width() - 2)
                output = truncate_display(output, width=width)
                line = Group(line, Text.from_markup(f"[dim]  {escape(output)}[/]"))
            return self._with_facts(line)

        result = output or "completed"
        line = Text.from_markup(f"[dim]· {escape(f'{detail}: {result}')}[/]")
        return self._with_facts(line)

    def _with_facts(self, output: RenderableType) -> RenderableType:
        facts = [
            self.step,
            elapsed(self.started_at, self.finished_at),
            f"exit {self.exit_code}" if self.exit_code is not None else "",
        ]
        fact_text = " · ".join(fact for fact in facts if fact)
        if not fact_text:
            return output
        return Group(output, Text.from_markup(f"[dim]  {escape(fact_text)}[/]"))

    @staticmethod
    def _output_messages(event: StepEnd) -> list[str]:
        messages: list[str] = []
        for part in event.output:
            if not isinstance(part, ToolResultPart):
                continue
            stdout = as_text(part.output.get("stdout"))
            stderr = as_text(part.output.get("stderr"))
            if stdout:
                messages.append(stdout)
            if stderr:
                messages.append(stderr)
            if not stdout and not stderr and part.output:
                messages.append(_plain_value(part.output))
        return messages


@dataclass(frozen=True, slots=True)
class HeaderBlock:
    model_label: str
    home: str
    version_label: str

    def render(self) -> RenderableType:
        rows = [
            Text.from_markup(
                f"[dim]T··⅃ [/][bold]Toolang[/][dim] (v{escape(self.version_label)})[/]"
            ),
            Text(),
            Text.from_markup(f"[none]model: {escape(self.model_label)}[/]"),
            Text.from_markup(f"[none]home:  {escape(self.home)}[/]"),
        ]
        content = Table.grid(padding=0)
        content.add_column(no_wrap=True)
        for row in rows:
            content.add_row(row)
        return Group(
            Panel(
                content,
                box=box.ROUNDED,
                border_style="dim",
                padding=(0, 1),
                width=max(row.cell_len for row in rows) + 4,
            ),
            Text("\n"),
        )


@dataclass(frozen=True, slots=True)
class SlashBlock:
    message: str
    body: Sequence[str]

    def render(self) -> RenderableType:
        lines: list[RenderableType] = [bar([], style="white on grey23")]
        lines.extend(
            bar(
                [(">", "grey70 on grey23"), (f" {line}", "white on grey23")]
                if index == 0
                else [(f"  {line}", "white on grey23")]
            )
            for index, line in enumerate(self.message.splitlines() or [""])
        )
        lines.extend([bar([], style="white on grey23"), Text()])
        if self.body:
            first, *rest = self.body
            lines.append(Text.from_markup(f"[dim]:[/] [bold]{escape(first)}[/]"))
            lines.append(Text())
            if rest and not rest[0].strip():
                rest = rest[1:]
            lines.extend(self._body_line(line) for line in rest)
        lines.append(Text("\n"))
        return Group(*lines)

    @staticmethod
    def _body_line(line: str) -> Text:
        if not line.strip():
            return Text()
        if line.startswith(":"):
            return SlashBlock._command_line(line)
        columns = _split_columns(line)
        if len(columns) > 1:
            return SlashBlock._table_line(columns)
        return Text.from_markup(f"[none]  {escape(line)}[/]")

    @staticmethod
    def _command_line(line: str) -> Text:
        usage, _, summary = line.partition("  ")
        while summary.startswith(" "):
            summary = summary[1:]
        text = Text("  ")
        SlashBlock._append_usage(text, usage)
        if summary:
            pad = max(2, 34 - text.cell_len)
            text.append(" " * pad)
            text.append(summary, style="none")
        return text

    @staticmethod
    def _table_line(columns: Sequence[str]) -> Text:
        text = Text("  ")
        first, *rest = columns
        badge = rest[0] if rest and rest[0] in {"current", "default"} else ""
        details = rest[1:] if badge else rest
        text.append(first, style="cyan")
        first_width = 40
        text.append(" " * max(2, first_width - display_len(first)))
        if badge:
            text.append(badge, style="yellow" if badge == "default" else "dim")
        text.append(" " * max(2, 9 - display_len(badge)))
        for index, column in enumerate(details):
            text.append(column, style="dim")
            if index < len(details) - 1:
                text.append("  ", style="none")
        return text

    @staticmethod
    def _append_usage(text: Text, usage: str) -> None:
        for index, token in enumerate(usage.split(" ")):
            if index:
                text.append(" ")
            style = "cyan" if token.startswith(":") else "dim"
            if token.startswith(":"):
                command, separator, rest = token.partition(",")
                text.append(command, style=style)
                if separator:
                    text.append(separator, style="dim")
                    text.append(rest, style="cyan" if rest.startswith(":") else "dim")
            else:
                text.append(token, style=style)


def _split_columns(line: str) -> list[str]:
    return [part for part in re.split(r" {2,}", line.strip()) if part]


def _plain_value(value: object) -> str:
    if isinstance(value, str):
        return summarize(value, width=160)
    if isinstance(value, (int, float, bool)) or value is None:
        return str(value)
    return summarize(
        json.dumps(value, ensure_ascii=False, separators=(",", ":")), width=160
    )


def _tool_call_display_from_parts(parts: Sequence[MessagePart]) -> str:
    for part in parts:
        if isinstance(part, ToolCallPart):
            return _tool_call_display(
                part.tool_name or part.tool_family or "tool",
                dict(part.input),
            )
        if isinstance(part, ToolResultPart):
            return part.tool_name or part.tool_family or "tool"
    return "tool"


def _tool_call_display(name: str, tool_input: dict[str, Any]) -> str:
    if not tool_input:
        return name
    for key in ("command", "cmd", "query", "path", "url", "prompt", "text"):
        if (value := tool_input.get(key)) is not None:
            return f"{name}: {_plain_value(value)}"
    if len(tool_input) == 1:
        return f"{name}: {_plain_value(next(iter(tool_input.values())))}"
    summary = ", ".join(
        f"{key}={_plain_value(value)}" for key, value in tool_input.items()
    )
    return f"{name}: {summary}"


def _parts_text(parts: Sequence[MessagePart]) -> str:
    return "".join(part.text for part in parts if isinstance(part, TextPart)).strip()
