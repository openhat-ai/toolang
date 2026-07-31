"""Event router for script execution presentation blocks."""

from __future__ import annotations

from collections.abc import Mapping
import sys
from typing import TextIO

from toolang.base.types.message import Percept, TextDelta
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

from .blocks import CallBlock, RunBlock, StatementBlock
from .console import ProgressConsole
from .formatting import (
    integer,
    runtime_failure,
    statement_title,
)


class ConsoleRunTracer(RunTracer):
    """Render one script run through event-driven presentation blocks."""

    def __init__(
        self,
        *,
        run_id: str,
        verbosity: int = 0,
        stream: TextIO | None = None,
        runnable_kind: str | None = None,
        runnable_name: str | None = None,
        runnable_doc: str | None = None,
        input_value: Percept = (),
        args: Mapping[str, object] | None = None,
        width: int | None = None,
    ) -> None:
        self.run_id = run_id
        self.verbosity = max(0, verbosity)
        self.runnable_kind = (runnable_kind or "").strip()
        self.runnable_name = (runnable_name or "").strip()
        self.runnable_doc = (runnable_doc or "").strip()
        self.input_value = input_value
        self.args = dict(args or {})
        self.console = ProgressConsole(stream or sys.stderr, width=width)
        self._runs: dict[str, RunBlock] = {}
        self._statements: dict[str, StatementBlock] = {}
        self._calls: dict[str, CallBlock] = {}
        self._outcomes: dict[str, StepEnd] = {}
        self._reported_errors: set[str] = set()

    async def on_event(self, event: RunEvent) -> None:
        if isinstance(event, RunBegin):
            self._begin_run(event)
        elif isinstance(event, StepBegin):
            self._begin_step(event)
        elif isinstance(event, PartDelta):
            self._part_delta(event)
        elif isinstance(event, StepEnd):
            self._end_step(event)
        elif isinstance(event, RunEnd):
            self._end_run(event)

    def close(self) -> None:
        """Remove the bounded live area without changing stable scrollback."""

        self.console.close()

    def _begin_run(self, event: RunBegin) -> None:
        owner = self._statements.get(event.parent or "")
        indent = owner.content_indent if owner is not None else 0
        run = RunBlock.from_event(event, indent=indent)
        self._runs[event.run] = run
        if event.run == self.run_id:
            run.render_header(
                self.console,
                verbosity=self.verbosity,
                kind=self.runnable_kind,
                name=self.runnable_name,
                doc=self.runnable_doc,
                input_value=self.input_value,
                args=self.args,
                control_index=event.input.index,
            )
            return
        if owner is None:
            return
        owner.child_started(run)
        owner.render_work(self.console, run)
        live_owner = self._live_owner(owner)
        if owner.hidden:
            live_owner.active_run = run.run_id
            live_owner.active_item = integer(run.placement.get("loop"))
            live_owner.active_activity = owner.work_line(run)
        self._show_statement_live(live_owner, event.started_at)

    def _end_run(self, event: RunEnd) -> None:
        run = self._runs.get(event.run)
        if run is None:
            return
        run.finish(event)
        if event.run == self.run_id:
            output = self._outcomes.get(event.output.step) if event.output else None
            run.render_result(
                self.console,
                event,
                output=output,
                error=self._new_error(event.error),
            )
            return
        owner = self._statements.get(run.parent or "")
        if owner is None:
            return
        owner.child_finished(run)
        parent_run = self._runs.get(trace_run(owner.begin.step))
        if parent_run is not None:
            parent_run.metrics.add(run.metrics)
        live_owner = self._live_owner(owner)
        if owner.hidden and live_owner.active_run == run.run_id:
            live_owner.active_run = None
            live_owner.active_activity = "iteration completed"
        self._show_statement_live(live_owner, event.finished_at)

    def _begin_step(self, event: StepBegin) -> None:
        if runtime_failure(event):
            return
        statement = event.given.get("statement")
        if isinstance(statement, str) and statement:
            run = self._runs.get(trace_run(event.step))
            nesting = max(event.step.count("/") - 1, 0) * 2
            base_indent = (run.indent if run is not None else 0) + nesting
            live_owner = self._repeat_owner(event.step, run)
            block = StatementBlock(
                event,
                base_indent=base_indent,
                hidden=live_owner is not None,
                live_owner=live_owner.begin.step if live_owner is not None else None,
            )
            self._statements[event.step] = block
            block.render_begin(self.console, verbosity=self.verbosity)
            if live_owner is not None:
                placement = event.given.get("placement")
                if isinstance(placement, Mapping):
                    live_owner.active_item = integer(placement.get("loop"))
                live_owner.active_activity = statement_title(event.given)
                self._show_statement_live(live_owner, event.started_at)
            return
        block = CallBlock(event)
        self._calls[event.step] = block
        run = self._runs.get(trace_run(event.step))
        owner = self._activity_owner(run)
        block.render_begin(
            self.console,
            indent=run.indent if run is not None else 0,
            batched=owner.batched if owner is not None else False,
        )
        if owner is not None and owner.batched:
            owner.set_activity(trace_run(event.step), block.active_label)
            self._show_statement_live(owner, event.started_at)

    def _part_delta(self, event: PartDelta) -> None:
        if not isinstance(event.delta, TextDelta):
            return
        block = self._calls.get(event.step)
        if block is None:
            return
        run = self._runs.get(trace_run(event.step))
        owner = self._activity_owner(run)
        value = block.render_delta(
            self.console,
            event.delta.text,
            indent=run.indent if run is not None else 0,
            batched=owner.batched if owner is not None else False,
        )
        if owner is not None and owner.batched:
            owner.set_activity(trace_run(event.step), value or "responding…")
            self._show_statement_live(owner, "")

    def _end_step(self, event: StepEnd) -> None:
        self._outcomes[event.step] = event
        run = self._runs.get(trace_run(event.step))
        self._record_metrics(run, event)
        if statement := self._statements.get(event.step):
            statement.render_end(
                self.console,
                event,
                verbosity=self.verbosity,
                error="" if statement.hidden else self._new_error(event.error),
            )
            if statement.hidden:
                owner = self._live_owner(statement)
                owner.active_activity = (
                    "iteration completed"
                    if event.status == "finished"
                    else f"iteration {event.status}"
                )
                self._show_statement_live(owner, event.finished_at)
            return
        call = self._calls.get(event.step)
        if call is None:
            return
        owner = self._activity_owner(run)
        if owner is not None and owner.batched:
            owner.set_activity(trace_run(event.step), call.completed_label(event))
            self._show_statement_live(owner, event.finished_at)
        call.render_end(
            self.console,
            event,
            verbosity=self.verbosity,
            indent=run.indent if run is not None else 0,
            batched=owner.batched if owner is not None else False,
            error=self._new_error(event.error),
        )

    def _owner_statement(self, run: RunBlock | None) -> StatementBlock | None:
        if run is None or run.parent is None:
            return None
        return self._statements.get(run.parent)

    def _activity_owner(self, run: RunBlock | None) -> StatementBlock | None:
        owner = self._owner_statement(run)
        return self._live_owner(owner) if owner is not None else None

    def _live_owner(self, statement: StatementBlock) -> StatementBlock:
        if statement.live_owner is None:
            return statement
        return self._statements.get(statement.live_owner, statement)

    def _repeat_owner(
        self,
        step: str,
        run: RunBlock | None,
    ) -> StatementBlock | None:
        parent = trace_parent(step)
        direct = self._statements.get(parent) if parent is not None else None
        if direct is not None and direct.statement == "repeat":
            return direct
        owner = self._owner_statement(run)
        if owner is None:
            return None
        live_owner = self._live_owner(owner)
        return live_owner if live_owner.statement == "repeat" else None

    def _show_statement_live(
        self,
        statement: StatementBlock,
        timestamp: str,
    ) -> None:
        if not self.console.tty or not statement.batched:
            return
        lines = statement.live_lines(timestamp)
        if lines:
            self.console.show_live(lines)

    @staticmethod
    def _record_metrics(run: RunBlock | None, event: StepEnd) -> None:
        if run is None:
            return
        if event.kind == "model":
            run.metrics.model_calls += 1
            usage = event.noted.get("usage")
            if isinstance(usage, Mapping):
                run.metrics.input_tokens += integer(usage.get("input_tokens")) or 0
                run.metrics.output_tokens += integer(usage.get("output_tokens")) or 0
        elif event.kind == "tool":
            run.metrics.tool_calls += 1

    def _new_error(self, error: str | None) -> str:
        value = (error or "").strip()
        if not value or value in self._reported_errors:
            return ""
        self._reported_errors.add(value)
        return value
