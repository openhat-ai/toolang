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
from toolang.execution.records import StepOutputRef
from toolang.execution.types import StepPath

from .blocks import CallBlock, RunBlock, StatementBlock
from .console import ProgressConsole
from ..execution_progress.formatting import (
    integer,
    output_preview,
    runtime_failure,
    status_label,
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
        self._statements: dict[StepPath, StatementBlock] = {}
        self._calls: dict[StepPath, CallBlock] = {}
        self._outcomes: dict[StepPath, StepEnd] = {}
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
        owner = self._statements.get(event.parent) if event.parent is not None else None
        until = owner is not None and self._is_until(event, owner)
        indent = owner.content_indent + (2 if until else 0) if owner is not None else 0
        run = RunBlock.from_event(event)
        run.indent = indent
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
        if until:
            owner.render_until_begin(
                self.console,
                run,
                verbosity=self.verbosity,
            )
        else:
            owner.render_work(
                self.console,
                run,
                verbosity=self.verbosity,
            )
        live_owner = self._live_owner(owner)
        if owner.hidden:
            live_owner.active_run = run.run_id
            live_owner.active_item = integer(run.placement.get("loop"))
            live_owner.active_work = owner.work_line(run)
            live_owner.active_activity = "starting…"
        self._show_statement_live(live_owner, event.started_at)

    def _end_run(self, event: RunEnd) -> None:
        run = self._runs.get(event.run)
        if run is None:
            return
        run.finish(event)
        if event.run == self.run_id:
            output = (
                self._outcomes.get(event.output.step)
                if isinstance(event.output, StepOutputRef)
                else None
            )
            run.render_result(
                self.console,
                event,
                output=output,
                error=self._new_error(event.error),
            )
            return
        owner = self._statements.get(run.parent) if run.parent is not None else None
        if owner is None:
            return
        until = self._is_until_run(run, owner)
        owner.child_finished(run)
        parent_run = self._runs.get(owner.begin.step.run)
        if parent_run is not None:
            parent_run.metrics.add(run.metrics)
        individual = not owner.batched or until
        if individual and run.status != "finished":
            if error := self._new_error(event.error):
                self.console.wrapped(
                    f"{run.run_id} {status_label(run.status)}: {error}",
                    prefix=f"{' ' * run.indent}! ",
                    continuation=f"{' ' * (run.indent + 2)}",
                    tone="error" if run.status == "failed" else "warning",
                )
        if individual and (self.verbosity >= 2 or run.status != "finished"):
            run.render_compact(self.console, finished_at=event.finished_at)
        if until and run.status == "finished":
            owner.render_until_decision(
                self.console,
                self._until_decision(event),
                verbosity=self.verbosity,
            )
        live_owner = self._live_owner(owner)
        if owner.hidden and live_owner.active_run == run.run_id:
            live_owner.active_run = None
            live_owner.active_activity = f"↳ {run.run_id} {status_label(run.status)}"
        self._show_statement_live(live_owner, event.finished_at)

    def _begin_step(self, event: StepBegin) -> None:
        if runtime_failure(event):
            return
        statement = event.given.get("statement")
        if isinstance(statement, str) and statement:
            run = self._runs.get(event.step.run)
            nesting = max(len(event.step.indices) - 1, 0) * 2
            base_indent = (run.indent if run is not None else 0) + nesting
            direct_repeat = self._direct_repeat_owner(event.step)
            live_owner = self._repeat_owner(event.step, run)
            ordinal: int | None = None
            if direct_repeat is not None:
                placement = event.given.get("placement")
                iteration = (
                    integer(placement.get("loop"))
                    if isinstance(placement, Mapping)
                    else None
                )
                ordinal = direct_repeat.enter_iteration(
                    self.console,
                    iteration if iteration is not None else 0,
                    verbosity=self.verbosity,
                )
            block = StatementBlock(
                event,
                base_indent=base_indent,
                hidden=live_owner is not None and self.verbosity < 2,
                live_owner=live_owner.begin.step if live_owner is not None else None,
                ordinal=ordinal,
            )
            self._statements[event.step] = block
            block.render_begin(self.console, verbosity=self.verbosity)
            if direct_repeat is not None and block.hidden:
                direct_repeat.activate_nested(block)
                self._show_statement_live(direct_repeat, event.started_at)
            return
        block = CallBlock(event)
        self._calls[event.step] = block
        run = self._runs.get(event.step.run)
        owner = self._activity_owner(run)
        block.render_begin(
            self.console,
            indent=run.indent if run is not None else 0,
            batched=owner.batched if owner is not None else False,
        )
        if owner is not None and owner.batched:
            owner.set_activity(event.step.run, block.active_label)
            self._show_statement_live(owner, event.started_at)

    def _part_delta(self, event: PartDelta) -> None:
        if not isinstance(event.delta, TextDelta):
            return
        block = self._calls.get(event.step)
        if block is None:
            return
        run = self._runs.get(event.step.run)
        owner = self._activity_owner(run)
        value = block.render_delta(
            self.console,
            event.delta.text,
            indent=run.indent if run is not None else 0,
            batched=owner.batched if owner is not None else False,
        )
        if owner is not None and owner.batched:
            owner.set_activity(event.step.run, value or "responding…")
            self._show_statement_live(owner, "")

    def _end_step(self, event: StepEnd) -> None:
        self._outcomes[event.step] = event
        run = self._runs.get(event.step.run)
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
            owner.set_activity(event.step.run, call.completed_label(event))
            self._show_statement_live(owner, event.finished_at)
        immediate_owner = self._owner_statement(run)
        deferred_batch_failure = (
            event.status != "finished"
            and immediate_owner is not None
            and immediate_owner.batched
            and immediate_owner.statement != "repeat"
        )
        if deferred_batch_failure:
            return
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
        if owner is None:
            return None
        if self.verbosity >= 2 and run is not None and self._is_until_run(run, owner):
            return None
        if self.verbosity >= 2 and owner.live_owner is not None:
            return owner
        return self._live_owner(owner)

    def _live_owner(self, statement: StatementBlock) -> StatementBlock:
        if statement.live_owner is None:
            return statement
        return self._statements.get(statement.live_owner, statement)

    def _repeat_owner(
        self,
        step: StepPath,
        run: RunBlock | None,
    ) -> StatementBlock | None:
        if direct := self._direct_repeat_owner(step):
            return direct
        owner = self._owner_statement(run)
        if owner is None:
            return None
        live_owner = self._live_owner(owner)
        return live_owner if live_owner.statement == "repeat" else None

    def _direct_repeat_owner(self, step: StepPath) -> StatementBlock | None:
        parent = step.parent
        owner = self._statements.get(parent) if parent is not None else None
        return owner if owner is not None and owner.statement == "repeat" else None

    def _show_statement_live(
        self,
        statement: StatementBlock,
        timestamp: str,
    ) -> None:
        if (
            not self.console.tty
            or not statement.batched
            or (self.verbosity >= 2 and statement.statement == "repeat")
        ):
            return
        lines = statement.live_lines(timestamp)
        if lines:
            self.console.show_live(lines)

    def _until_decision(self, event: RunEnd) -> bool | None:
        if not isinstance(event.output, StepOutputRef):
            return None
        outcome = self._outcomes.get(event.output.step)
        if outcome is None:
            return None
        value = output_preview(outcome).strip()
        if value == "true":
            return True
        if value == "false":
            return False
        return None

    @staticmethod
    def _is_until(event: RunBegin, owner: StatementBlock) -> bool:
        placement = event.context.get("placement")
        return (
            owner.statement == "repeat"
            and isinstance(placement, Mapping)
            and placement.get("role") == "until"
        )

    @staticmethod
    def _is_until_run(run: RunBlock, owner: StatementBlock) -> bool:
        return owner.statement == "repeat" and run.placement.get("role") == "until"

    @staticmethod
    def _record_metrics(run: RunBlock | None, event: StepEnd) -> None:
        if run is None:
            return
        if event.kind == "model":
            run.metrics.model_calls += 1
            tokens = event.noted.get("tokens")
            if isinstance(tokens, Mapping):
                run.metrics.input_tokens += integer(tokens.get("input")) or 0
                run.metrics.output_tokens += integer(tokens.get("output")) or 0
        elif event.kind == "tool":
            run.metrics.tool_calls += 1

    def _new_error(self, error: str | None) -> str:
        value = (error or "").strip()
        if not value or value in self._reported_errors:
            return ""
        self._reported_errors.add(value)
        return value
