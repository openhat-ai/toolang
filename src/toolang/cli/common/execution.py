"""Local execution resources owned by one CLI command."""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
import sys
from typing import Any, Iterator, TextIO, cast

import click
import typer

from toolang.base.types.message import TextDelta, TextPart, ToolResultPart
from toolang.common.ids import IdIssuer
from toolang.execution.events import (
    PartDelta,
    RunBegin,
    RunEnd,
    RunEvent,
    RunTracer,
    StepBegin,
    StepEnd,
)
from toolang.execution.records import trace_parent, trace_run
from toolang.execution.store import RunStore

from .context import context_layout
from .output import parse_utc_timestamp


@dataclass(frozen=True, slots=True)
class ExecutionResources:
    """Process-local access to one agent's durable execution state."""

    store: RunStore
    ids: IdIssuer


@dataclass(slots=True)
class _RunProgress:
    run_id: str
    parent: str | None
    kind: str
    name: str
    placement: Mapping[str, object]
    started_at: str
    preview: str = ""

    @property
    def label(self) -> str:
        return f"{self.kind}:{self.name}" if self.name else self.kind


@dataclass(slots=True)
class _StepProgress:
    begin: StepBegin
    children: int = 0
    completed: int = 0
    failed: int = 0
    total: int | None = None
    failed_children: list[_RunProgress] = field(default_factory=list)
    lanes: dict[int, int] = field(default_factory=dict)
    lane_count: int | None = None


class ConsoleRunTracer(RunTracer):
    """Render semantic agic and flow progress for one script run."""

    def __init__(
        self,
        *,
        run_id: str,
        verbosity: int = 0,
        stream: TextIO | None = None,
    ) -> None:
        self.run_id = run_id
        self.verbosity = max(0, verbosity)
        self.stream = stream or sys.stderr
        self._tty = bool(
            getattr(self.stream, "isatty", lambda: False)()
        )
        self._runs: dict[str, _RunProgress] = {}
        self._steps: dict[str, _StepProgress] = {}
        self._previews: dict[str, str] = {}
        self._live_line = False
        self._reported_errors: set[str] = set()

    async def on_event(self, event: RunEvent) -> None:
        if isinstance(event, RunBegin):
            self._begin_run(event)
            return
        if isinstance(event, StepBegin):
            self._begin_step(event)
            return
        if isinstance(event, PartDelta):
            self._update_preview(event)
            return
        if isinstance(event, StepEnd):
            self._end_step(event)
            return
        if isinstance(event, RunEnd):
            self._end_run(event)

    def close(self) -> None:
        """Clear any transient terminal line."""

        self._clear_live()

    def _begin_run(self, event: RunBegin) -> None:
        runnable = _mapping(event.context.get("runnable"))
        progress = _RunProgress(
            run_id=event.run,
            parent=event.parent,
            kind=_text(runnable.get("kind")) or "run",
            name=_text(runnable.get("name")) or "",
            placement=_mapping(event.context.get("placement")),
            started_at=event.started_at,
        )
        self._runs[event.run] = progress
        if event.run == self.run_id:
            self._write(f"→ run {event.run} · {progress.label}")
            return
        if event.parent is not None:
            self._start_child(event.parent, progress)

    def _end_run(self, event: RunEnd) -> None:
        progress = self._runs.get(event.run)
        if progress is None:
            return
        if event.run != self.run_id:
            if progress.parent is not None:
                self._finish_child(progress.parent, progress, event)
            self._runs.pop(event.run, None)
            return

        marker = _status_mark(event.status)
        facts = [_elapsed(progress.started_at, event.finished_at)]
        error = self._new_error(event.error)
        suffix = f": {error}" if error else ""
        status = _run_status_label(event.status)
        self._write(f"{marker} run {status}{_fact_suffix(facts)}{suffix}")

    def _begin_step(self, event: StepBegin) -> None:
        progress = _StepProgress(begin=event)
        self._steps[event.step] = progress
        if _runtime_failure(event):
            return
        if not self._show_step(event.step):
            return
        label = _step_label(event.kind, event.given)
        facts: list[str] = []
        if self.verbosity:
            detail = _statement_detail(event.given)
            if detail and detail.casefold() != label.casefold():
                facts.append(detail)
            facts.append(_source_label(event.given))
        rendered = (
            f"{self._step_lead(event.step, '…')}{label}{_fact_suffix(facts)}"
        )
        if self._tty:
            self._show_live(rendered)
        else:
            self._write(rendered.replace("…", "→", 1))

    def _end_step(self, event: StepEnd) -> None:
        progress = self._steps.get(event.step)
        self._previews.pop(event.step, None)
        if progress is None:
            return
        visible = self._show_step(event.step)
        if visible:
            self._clear_live()
        run = self._runs.get(trace_run(event.step))
        event_preview = _output_preview(event)
        if run is not None and event_preview:
            run.preview = event_preview
        begin = progress.begin
        if _runtime_failure(begin) and event.error in self._reported_errors:
            self._steps.pop(event.step, None)
            return
        if visible:
            marker = _status_mark(event.status)
            label = _step_label(event.kind, begin.given)
            facts = self._step_facts(progress, event)
            error = self._new_error(event.error)
            suffix = f": {error}" if error else ""
            self._write(
                f"{self._step_lead(event.step, marker)}"
                f"{label}{_fact_suffix(facts)}{suffix}"
            )
            if self.verbosity >= 3:
                preview = event_preview
                if not preview and progress.failed_children:
                    preview = progress.failed_children[0].preview
                if preview:
                    self._write(f"{self._step_indent(event.step)}  output: {preview}")
        self._steps.pop(event.step, None)

    def _start_child(self, parent: str, run: _RunProgress) -> None:
        owner = self._steps.get(parent)
        if owner is None:
            return
        owner.children += 1
        total = _integer(run.placement.get("items"))
        if total is not None:
            owner.total = max(owner.total or 0, total)
        lane = _integer(run.placement.get("lane"))
        lanes = _integer(run.placement.get("lanes"))
        item = _integer(run.placement.get("item"))
        if lane is not None and item is not None:
            owner.lanes[lane] = item
        if lanes is not None:
            owner.lane_count = max(owner.lane_count or 0, lanes)
        if owner.begin.kind == "par":
            self._show_parallel_live(owner)

    def _finish_child(
        self,
        parent: str,
        run: _RunProgress,
        event: RunEnd,
    ) -> None:
        owner = self._steps.get(parent)
        if owner is None:
            return
        owner.completed += 1
        if event.status == "failed":
            owner.failed += 1
            owner.failed_children.append(run)
        lane = _integer(run.placement.get("lane"))
        if lane is not None:
            owner.lanes.pop(lane, None)
        if owner.begin.kind == "par":
            self._show_parallel_live(owner)

    def _update_preview(self, event: PartDelta) -> None:
        if not self._tty or not isinstance(event.delta, TextDelta):
            return
        if not self._show_live_step(event.step):
            return
        text = self._previews.get(event.step, "") + event.delta.text
        self._previews[event.step] = text[-800:]
        preview = _one_line(text)
        if preview:
            self._show_live(f"  • {_truncate(preview, 120)}")

    def _show_step(self, step: str) -> bool:
        run_id = trace_run(step)
        if run_id != self.run_id:
            return False
        return trace_parent(step) == self.run_id or self.verbosity >= 2

    def _show_live_step(self, step: str) -> bool:
        return trace_run(step) == self.run_id

    def _show_parallel_live(self, progress: _StepProgress) -> None:
        if not self._tty:
            return
        label = _step_label(progress.begin.kind, progress.begin.given)
        total = progress.total or progress.children
        facts = [f"{progress.completed}/{total}"]
        if progress.lanes:
            lane_items = " ".join(
                f"L{lane + 1}→{item + 1}"
                for lane, item in sorted(progress.lanes.items())
            )
            facts.append(lane_items)
        elif progress.lane_count:
            facts.append(f"{progress.lane_count} lanes")
        self._show_live(
            f"{self._step_lead(progress.begin.step, '…')}"
            f"{label}{_fact_suffix(facts)}"
        )

    def _step_lead(self, step: str, marker: str) -> str:
        run_id = trace_run(step)
        if run_id == self.run_id:
            relative = step.removeprefix(f"{self.run_id}/")
            depth = relative.count("/") + 1
            return f"{'  ' * depth}{marker} step {relative} · "
        run = self._runs.get(run_id)
        placement = (
            self._placement_label(run.placement) if run is not None else ""
        )
        identity = f"{step} " if self.verbosity >= 3 else ""
        context = f"{placement + ' ' if placement else ''}{identity}"
        return f"  {marker} step {context}"

    def _step_indent(self, step: str) -> str:
        if trace_run(step) != self.run_id:
            return "  "
        relative = step.removeprefix(f"{self.run_id}/")
        return "  " * (relative.count("/") + 1)

    def _step_facts(
        self,
        progress: _StepProgress,
        event: StepEnd,
    ) -> list[str]:
        given = progress.begin.given
        noted = event.noted
        statement = _text(given.get("statement"))
        items = _integer(noted.get("items"))
        total = progress.total or progress.children or None
        facts: list[str] = []
        if event.status == "failed" and progress.failed_children:
            failed = progress.failed_children[0]
            facts.append(self._placement_label(failed.placement))
            if self.verbosity >= 2:
                facts.append(failed.run_id)
        elif statement == "keep" and items is not None and total is not None:
            facts.append(f"kept {items}/{total}")
        elif statement == "drop" and items is not None and total is not None:
            facts.append(f"retained {items}/{total}")
        elif statement in {"map", "storm"} and total is not None:
            facts.append(f"{progress.completed}/{total} items")
        elif items is not None:
            facts.append(f"{items} item{'s' if items != 1 else ''}")
        elif progress.children:
            facts.append(f"{progress.completed}/{total or progress.children} items")
        if event.kind == "model":
            facts.extend(_usage_facts(noted))
        if event.kind == "tool":
            facts.extend(_tool_facts(event))
        if self.verbosity:
            shape = _text(noted.get("shape"))
            if shape and not items:
                facts.append(f"shape={shape}")
        if self.verbosity or event.status == "failed":
            facts.append(_source_label(given))
        facts.append(_elapsed(progress.begin.started_at, event.finished_at))
        return facts

    @staticmethod
    def _placement_label(placement: Mapping[str, object]) -> str:
        item = _integer(placement.get("item"))
        items = _integer(placement.get("items"))
        if item is not None and items is not None:
            return f"item {item + 1}/{items}"
        return ""

    def _new_error(self, error: str | None) -> str:
        text = (error or "").strip()
        if not text or text in self._reported_errors:
            return ""
        self._reported_errors.add(text)
        return text

    def _write(self, value: str) -> None:
        self._clear_live()
        print(value, file=self.stream, flush=True)

    def _show_live(self, value: str) -> None:
        if not self._tty:
            return
        self.stream.write(f"\r\x1b[2K{_truncate(value, 140)}")
        self.stream.flush()
        self._live_line = True

    def _clear_live(self) -> None:
        if not self._live_line:
            return
        self.stream.write("\r\x1b[2K")
        self.stream.flush()
        self._live_line = False


def _mapping(value: object) -> Mapping[str, Any]:
    return cast(Mapping[str, Any], value) if isinstance(value, Mapping) else {}


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _integer(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _fact_suffix(facts: list[str]) -> str:
    values = [value for value in facts if value]
    return f" · {' · '.join(values)}" if values else ""


def _status_mark(status: str) -> str:
    return {
        "finished": "✓",
        "failed": "✗",
        "canceled": "−",
        "running": "…",
    }.get(status, "·")


def _run_status_label(status: str) -> str:
    return {
        "finished": "completed",
        "failed": "failed",
        "canceled": "canceled",
        "running": "running",
    }.get(status, status)


def _step_label(kind: str, given: Mapping[str, Any]) -> str:
    doc = _text(given.get("doc"))
    if doc:
        return _one_line(doc)
    statement = _text(given.get("statement"))
    runnable = _text(given.get("runnable"))
    agent = _text(given.get("agent"))
    binding = _text(given.get("binding"))
    labels = {
        "run": f"Run {runnable}" if runnable else "Run child",
        "seek": (
            f"Seek {agent}/{runnable}"
            if agent and runnable
            else f"Seek {agent or runnable}".rstrip()
        ),
        "ask": "Request human input",
        "scatter": f"Scatter with {runnable}".rstrip(),
        "storm": f"Generate with {runnable}".rstrip(),
        "gather": f"Gather with {runnable}".rstrip(),
        "settle": f"Settle with {runnable}".rstrip(),
        "map": f"Map with {runnable}".rstrip(),
        "keep": "Keep matching items",
        "drop": "Drop matching items",
        "rank": "Rank items",
        "repeat": "Repeat",
        "let": f"Set {binding or 'value'}",
    }
    if statement:
        return labels.get(statement, statement)
    if kind == "model":
        model = _mapping(given.get("model"))
        target = _text(model.get("ref")) or _text(model.get("model"))
        return f"model {target}".rstrip()
    if kind == "tool":
        tool = _text(given.get("tool"))
        return f"tool {tool}".rstrip()
    if kind == "system" and _runtime_failure_data(given):
        return "output coercion"
    return kind


def _statement_detail(given: Mapping[str, Any]) -> str:
    statement = _text(given.get("statement"))
    if not statement:
        return ""
    values = [statement]
    if statement in {"scatter", "storm", "repeat"}:
        count = _integer(given.get("count"))
        if count is not None:
            values.append(str(count))
    limit = _text(given.get("limit"))
    if limit:
        values.append(limit)
        count = _integer(given.get("count"))
        if count is not None:
            values.append(str(count))
    target = (
        _text(given.get("runnable"))
        or _text(given.get("predicate"))
        or _text(given.get("agent"))
    )
    if target:
        values.append(target)
    par = _integer(given.get("par"))
    if par is not None:
        values.extend(("par", str(par)))
    return " ".join(values)


def _source_label(given: Mapping[str, Any]) -> str:
    line = _integer(_mapping(given.get("source")).get("line"))
    return f"line {line}" if line is not None else ""


def _usage_facts(noted: Mapping[str, Any]) -> list[str]:
    usage = _mapping(noted.get("usage"))
    input_tokens = _integer(usage.get("input_tokens"))
    output_tokens = _integer(usage.get("output_tokens"))
    if input_tokens is None and output_tokens is None:
        return []
    return [f"{_compact_count(input_tokens or 0)}/{_compact_count(output_tokens or 0)} tokens"]


def _tool_facts(event: StepEnd) -> list[str]:
    for part in event.output:
        if not isinstance(part, ToolResultPart):
            continue
        results = part.output.get("results")
        if isinstance(results, list | tuple):
            return [f"{len(results)} results"]
        code = part.output.get("returncode")
        if isinstance(code, int):
            return [f"exit {code}"]
    return []


def _output_preview(event: StepEnd) -> str:
    if event.kind != "model":
        return ""
    text = " ".join(
        part.text for part in event.output if isinstance(part, TextPart)
    )
    preview = _truncate(_one_line(text), 180)
    return f'"{preview}"' if preview else ""


def _runtime_failure(event: StepBegin) -> bool:
    return event.kind == "system" and _runtime_failure_data(event.given)


def _runtime_failure_data(given: Mapping[str, Any]) -> bool:
    return given.get("runtime") == "failure"


def _elapsed(started_at: str, finished_at: str) -> str:
    if not started_at or not finished_at:
        return ""
    start = parse_utc_timestamp(started_at)
    finish = parse_utc_timestamp(finished_at)
    if start is None or finish is None:
        return ""
    seconds = max((finish - start).total_seconds(), 0.0)
    if seconds < 1:
        return f"{max(round(seconds * 1000), 1)}ms"
    if seconds < 10:
        return f"{seconds:.1f}s"
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes, remaining = divmod(round(seconds), 60)
    return f"{minutes}m {remaining:02d}s"


def _compact_count(value: int) -> str:
    if value < 1000:
        return str(value)
    if value < 1_000_000:
        return f"{value / 1000:.1f}".rstrip("0").rstrip(".") + "k"
    return f"{value / 1_000_000:.1f}".rstrip("0").rstrip(".") + "m"


def _one_line(value: str) -> str:
    return " ".join(value.split())


def _truncate(value: str, width: int) -> str:
    if len(value) <= width:
        return value
    return f"{value[: max(width - 1, 0)].rstrip()}…"


@contextmanager
def open_execution(
    ctx: typer.Context,
    *,
    required: bool = False,
) -> Iterator[ExecutionResources | None]:
    """Open one agent's execution store without creating it for read-only access."""

    layout = context_layout(ctx)
    if not layout.run_store.is_file():
        if required:
            raise click.ClickException(
                f"execution history not found: {layout.name}"
            )
        yield None
        return
    store = RunStore(layout.run_store)
    try:
        yield ExecutionResources(store=store, ids=IdIssuer(layout.id_state))
    finally:
        store.close()
