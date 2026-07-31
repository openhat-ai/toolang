"""Stateful presentation blocks for ordered execution events."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from toolang.base.types.message import Percept
from toolang.execution.events import RunBegin, RunEnd, StepBegin, StepEnd

from .console import ProgressConsole, Tone
from .formatting import (
    active_step_label,
    binding_action,
    completed_step_label,
    count,
    elapsed,
    integer,
    mapping,
    model_label,
    one_line,
    output_preview,
    percept_lines,
    runnable_label,
    shape_label,
    statement_index,
    statement_result,
    statement_target,
    statement_title,
    status_label,
    text,
    token_fact,
    tool_exit_code,
    tool_label,
    tool_result,
    truncate,
    usage_facts,
    value_summary,
)


@dataclass(slots=True)
class Metrics:
    """Aggregate work performed by one run tree or statement."""

    runs: int = 0
    model_calls: int = 0
    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0

    def add(self, other: Metrics) -> None:
        self.runs += other.runs
        self.model_calls += other.model_calls
        self.tool_calls += other.tool_calls
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens

    def facts(
        self,
        *,
        duration: str = "",
        include_runs: bool = True,
    ) -> list[str]:
        facts = [duration]
        if include_runs and self.runs:
            facts.append(count(self.runs, "run"))
        if self.input_tokens or self.output_tokens:
            facts.append(token_fact(self.input_tokens, self.output_tokens))
        if self.model_calls:
            facts.append(count(self.model_calls, "model call"))
        if self.tool_calls:
            facts.append(count(self.tool_calls, "tool call"))
        return [fact for fact in facts if fact]


@dataclass(slots=True)
class RunBlock:
    """One root or recursive run block."""

    run_id: str
    parent: str | None
    kind: str
    name: str
    placement: Mapping[str, object]
    started_at: str
    indent: int
    metrics: Metrics = field(default_factory=lambda: Metrics(runs=1))
    status: str = "running"

    @classmethod
    def from_event(cls, event: RunBegin, *, indent: int) -> RunBlock:
        runnable = mapping(event.context.get("runnable"))
        return cls(
            run_id=event.run,
            parent=event.parent,
            kind=text(runnable.get("kind")) or "run",
            name=runnable_label(text(runnable.get("name"))),
            placement=mapping(event.context.get("placement")),
            started_at=event.started_at,
            indent=indent,
        )

    @property
    def label(self) -> str:
        return " ".join(value for value in (self.kind, self.name) if value)

    def finish(self, event: RunEnd) -> None:
        self.status = event.status

    def render_header(
        self,
        console: ProgressConsole,
        *,
        verbosity: int,
        kind: str,
        name: str,
        doc: str,
        input_value: Percept,
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
        input_lines = percept_lines(input_value)
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
        if event.status == "finished" and output is not None:
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


@dataclass(slots=True)
class LaneBlock:
    run_id: str
    item: int
    activity: str = "starting…"


@dataclass(slots=True)
class CallBlock:
    """One model, tool, or system step within an agic."""

    begin: StepBegin
    preview: str = ""

    @property
    def active_label(self) -> str:
        return active_step_label(self.begin)

    def completed_label(self, event: StepEnd) -> str:
        return completed_step_label(self.begin, event)

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
        self.preview = (self.preview + delta)[-800:]
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
        if batched and event.status == "finished":
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
        elif event.status != "finished" and error:
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
        facts = [event.step, duration, model, *usage_facts(event.noted)]
        if event.status != "finished":
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
                tone=tone,
            )
            return
        preview = output_preview(event)
        if verbosity >= 1 and preview:
            console.wrapped(
                preview,
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
            event.step,
            duration,
            f"exit {exit_code}" if exit_code is not None else "",
        ]
        if event.status != "finished":
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
                tone=tone,
            )
            return
        result = tool_result(event)
        if verbosity >= 1:
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
class StatementBlock:
    """One authored flow statement and its child-work presentation."""

    begin: StepBegin
    base_indent: int
    children: list[str] = field(default_factory=list)
    completed: int = 0
    failed: int = 0
    total: int | None = None
    lane_count: int | None = None
    lanes: dict[int, LaneBlock] = field(default_factory=dict)
    metrics: Metrics = field(default_factory=Metrics)
    work_written: bool = False
    active_run: str | None = None
    active_item: int | None = None
    active_activity: str = "starting…"
    hidden: bool = False
    live_owner: str | None = None

    @property
    def statement(self) -> str:
        return text(self.begin.given.get("statement"))

    @property
    def content_indent(self) -> int:
        return self.base_indent + 2

    @property
    def batched(self) -> bool:
        return self.begin.kind in {"par", "loop"}

    def render_begin(
        self,
        console: ProgressConsole,
        *,
        verbosity: int,
    ) -> None:
        if self.hidden:
            return
        console.blank()
        heading = f"[{statement_index(self.begin.step)}] {statement_title(self.begin.given)}"
        console.write(f"{' ' * self.base_indent}{heading}")
        if verbosity >= 1 and (doc := text(self.begin.given.get("doc"))):
            console.wrapped(doc, prefix=" " * self.content_indent)
            console.blank()

    def child_started(self, run: RunBlock) -> None:
        self.children.append(run.run_id)
        self.active_run = run.run_id
        item = integer(run.placement.get("item"))
        self.active_item = item
        self.active_activity = "starting…"
        if (total := integer(run.placement.get("items"))) is not None:
            self.total = max(self.total or 0, total)
        lane = integer(run.placement.get("lane"))
        lanes = integer(run.placement.get("lanes"))
        if lane is not None and item is not None:
            self.lanes[lane] = LaneBlock(run.run_id, item)
        if lanes is not None:
            self.lane_count = max(self.lane_count or 0, lanes)

    def child_finished(self, run: RunBlock) -> None:
        self.metrics.add(run.metrics)
        if run.status == "finished":
            self.completed += 1
        elif run.status == "failed":
            self.failed += 1
        lane = integer(run.placement.get("lane"))
        if lane is not None:
            self.lanes.pop(lane, None)
        if self.active_run == run.run_id:
            self.active_run = None
            self.active_item = None

    def render_work(
        self,
        console: ProgressConsole,
        run: RunBlock,
    ) -> None:
        if self.work_written or self.hidden:
            return
        self.work_written = True
        console.write(f"{' ' * self.content_indent}{self.work_line(run)}")

    def work_line(self, run: RunBlock | None = None) -> str:
        label = run.label if run is not None else self._unresolved_run_label()
        total = self.total or len(self.children)
        lanes = self.lane_count or integer(self.begin.given.get("par"))
        if self.begin.kind == "par":
            unit = "times" if self.statement == "storm" else "items"
            if unit == "items":
                details = count(total, "item")
            else:
                details = f"{total} times"
            if lanes is not None:
                details = f"{details}, {count(lanes, 'lane')}"
            return f"Run {label} in parallel ({details})"
        if self.statement == "settle":
            return f"Run {label} sequentially ({total} items, {total} calls)"
        return f"Run {label}"

    def _unresolved_run_label(self) -> str:
        target = statement_target(self.begin.given)
        return f"agic {target}" if target.startswith("<agic:") else target

    def set_activity(self, run_id: str, activity: str) -> None:
        if self.active_run == run_id:
            self.active_activity = activity
        for lane in self.lanes.values():
            if lane.run_id == run_id:
                lane.activity = activity

    def live_lines(self, timestamp: str) -> list[str]:
        base = " " * self.content_indent
        duration = elapsed(self.begin.started_at, timestamp)
        if self.begin.kind == "par":
            progress = [
                f"{self.completed} completed",
                f"{len(self.lanes)} active",
                f"{self.failed} failed",
                duration,
            ]
            lines = [f"{base}{' · '.join(value for value in progress if value)}"]
            width = len(str(max((self.lane_count or 1) - 1, 0)))
            for lane_index, lane in sorted(self.lanes.items()):
                lines.append(
                    f"{base}{lane_index:>{width}} │ "
                    f"item {lane.item} | {lane.activity}"
                )
            return lines
        if self.statement == "settle":
            progress = [f"{count(self.completed, 'call')} completed"]
            if self.active_item is not None:
                progress.append(f"item {self.active_item} active")
            if duration:
                progress.append(duration)
            lines = [f"{base}{' · '.join(progress)}"]
            if self.active_run is not None:
                lines.append(f"{base}· {self.active_activity}")
            return lines
        if self.statement == "repeat":
            total = integer(self.begin.given.get("count"))
            progress = [
                (
                    f"iteration {self.active_item}"
                    if self.active_item is not None
                    else "starting…"
                ),
                f"{total} total" if total is not None else "",
            ]
            lines = [f"{base}{' · '.join(value for value in progress if value)}"]
            if self.active_activity:
                lines.append(f"{base}· {self.active_activity}")
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
        if event.status != "finished":
            if error:
                tone = _tone(event.status)
                console.wrapped(
                    f"{event.step} {status_label(event.status)}: {error}",
                    prefix=f"{' ' * self.content_indent}! ",
                    continuation=f"{' ' * (self.content_indent + 2)}",
                    tone=tone,
                )
                facts = [
                    elapsed(self.begin.started_at, event.finished_at),
                    f"{self.completed} completed" if self.completed else "",
                    f"{self.failed} failed" if self.failed else "",
                ]
                console.write(
                    f"{' ' * (self.content_indent + 2)}· "
                    f"{' · '.join(fact for fact in facts if fact)}",
                    tone=tone,
                )
            return
        if not self.work_written and statement_target(self.begin.given):
            self.work_written = True
            console.write(
                f"{' ' * self.content_indent}{self.work_line()}"
            )
        if verbosity >= 1 and self.begin.kind == "par":
            self._render_parallel_result(console, event)
        if verbosity < 2:
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
        console.blank()
        action = binding_action(self.begin.given)
        if action:
            console.write(f"{' ' * self.content_indent}{action}")
        console.wrapped(
            result,
            prefix=f"{' ' * self.content_indent}· ",
            continuation=f"{' ' * (self.content_indent + 2)}",
        )

    def _render_parallel_result(
        self,
        console: ProgressConsole,
        event: StepEnd,
    ) -> None:
        direct_runs = len(self.children)
        if direct_runs:
            facts = [f"{count(self.completed, 'run')} succeeded"]
            facts.extend(
                self.metrics.facts(
                    duration=elapsed(self.begin.started_at, event.finished_at),
                    include_runs=False,
                )
            )
        else:
            facts = ["0 runs", "empty input list"]
        console.wrapped(
            " · ".join(facts),
            prefix=f"{' ' * self.content_indent}· ",
            continuation=f"{' ' * (self.content_indent + 2)}",
        )


def _tone(status: str) -> Tone:
    if status == "failed":
        return "error"
    if status == "canceled":
        return "warning"
    return "progress"
