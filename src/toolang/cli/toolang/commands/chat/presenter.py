"""Conversation-oriented presentation of ordered native run events."""

from __future__ import annotations

from collections.abc import Mapping

from toolang.base.types.message import TextDelta
from toolang.execution.events import (
    PartBegin,
    PartDelta,
    PartEnd,
    RunBegin,
    RunEnd,
    RunEvent,
    StepBegin,
    StepEnd,
)
from toolang.execution.records import StepOutputRef
from toolang.execution.types import StepPath

from toolang.cli.common.execution_progress.formatting import (
    integer,
    one_line,
    output_preview,
    runtime_failure,
    status_label,
    truncate,
)
from toolang.cli.common.execution_progress.state import (
    CallState,
    RunState,
    StatementState,
)

from . import blocks
from .base import AppContext, friendly_error


class ChatRunPresenter:
    """Own chat visibility and block lifetime for one active root run."""

    def __init__(self) -> None:
        self._root_run_id: str | None = None
        self._runs: dict[str, RunState] = {}
        self._statements: dict[StepPath, StatementState] = {}
        self._calls: dict[StepPath, CallState] = {}
        self._outcomes: dict[StepPath, StepEnd] = {}
        self._blocks: dict[StepPath, blocks.MutableBlock] = {}
        self._reported_errors: set[str] = set()

    def handle(self, event: RunEvent, app: AppContext) -> None:
        if isinstance(event, RunBegin):
            self._begin_run(event, app)
        elif isinstance(event, StepBegin):
            self._begin_step(event, app)
        elif isinstance(event, PartDelta):
            self._part_delta(event)
        elif isinstance(event, StepEnd):
            self._end_step(event, app)
        elif isinstance(event, RunEnd):
            self._end_run(event, app)
        elif isinstance(event, (PartBegin, PartEnd)):
            return

    def handle_error(self, app: AppContext, message: str) -> bool:
        """Close an accepted or pending run after a local execution error."""

        active_run_id = app.get_active_run()
        if active_run_id is None and not app.get_live_blocks():
            return False
        if active_run_id is None and self._root_run_id is None:
            for block in list(app.get_live_blocks()):
                if isinstance(block, blocks.RunStartBlock):
                    app.finalize_block(block)
                else:
                    self._discard(block, app)
            app.finalize_block(blocks.SubmissionErrorBlock(message))
            self.reset()
            app.finish_run()
            return True
        for block in list(app.get_live_blocks()):
            if isinstance(block, (blocks.RunStartBlock, blocks.RunSteerBlock)):
                app.finalize_block(block)
            else:
                self._discard(block, app)
        app.finalize_block(
            blocks.RunStopBlock(
                run_id=active_run_id or self._root_run_id or "run",
                status="failed",
                error=friendly_error(message),
            )
        )
        app.finish_run()
        self.reset()
        return True

    def reset(self) -> None:
        self._root_run_id = None
        self._runs.clear()
        self._statements.clear()
        self._calls.clear()
        self._outcomes.clear()
        self._blocks.clear()
        self._reported_errors.clear()

    def _begin_run(self, event: RunBegin, app: AppContext) -> None:
        run = RunState.from_event(event)
        self._runs[event.run] = run
        root = event.parent is None
        if root:
            if app.get_active_run() not in {None, event.run}:
                return
            self._root_run_id = event.run
            app.set_active_run(event.run)
            self._finalize_commands(app, blocks.RunStartBlock, event)
            stop = blocks.RunStopBlock.create(event)
            self._append_tail(stop, app)
            return

        owner = self._statements.get(event.parent) if event.parent is not None else None
        if owner is None:
            return
        owner.child_started(run)
        owner.active_work = owner.work_line(run)
        if self._is_until(run, owner):
            owner.begin_until(run)
        live_owner = self._live_owner(owner)
        if owner.live_owner is not None:
            live_owner.active_run = run.run_id
            live_owner.active_item = integer(run.placement.get("loop"))
            live_owner.active_work = owner.work_line(run)
            live_owner.active_activity = "starting…"

    def _begin_step(self, event: StepBegin, app: AppContext) -> None:
        self._finalize_commands(app, blocks.RunSteerBlock, event)
        self._discard_pending_models(event.step, app)
        if runtime_failure(event):
            return
        statement = event.given.get("statement")
        if isinstance(statement, str) and statement:
            self._begin_statement(event, app)
            return
        call = CallState(event)
        self._calls[event.step] = call
        owner = self._activity_owner(event.step.run)
        if owner is not None:
            owner.set_activity(event.step.run, call.active_label)
            if not owner.batched:
                block = self._blocks.get(owner.begin.step)
                if isinstance(block, blocks.FlowStepBlock):
                    block.note_call(call)
            return
        block = self._call_block(event)
        self._blocks[event.step] = block
        self._insert_before_run(block, app)

    def _begin_statement(self, event: StepBegin, app: AppContext) -> None:
        run = self._runs.get(event.step.run)
        direct_repeat = self._direct_repeat_owner(event.step)
        live_owner = direct_repeat or self._repeat_owner(run)
        ordinal: int | None = None
        if direct_repeat is not None:
            placement = event.given.get("placement")
            iteration = (
                integer(placement.get("loop"))
                if isinstance(placement, Mapping)
                else None
            )
            ordinal = direct_repeat.note_iteration(iteration or 0)
        state = StatementState(
            event,
            live_owner=live_owner.begin.step if live_owner is not None else None,
            ordinal=ordinal,
        )
        self._statements[event.step] = state
        if live_owner is not None:
            live_owner.activate_nested(state)
            return
        block = blocks.FlowStepBlock.from_state(state)
        self._blocks[event.step] = block
        self._insert_before_run(block, app)

    def _part_delta(self, event: PartDelta) -> None:
        if not isinstance(event.delta, TextDelta):
            return
        call = self._calls.get(event.step)
        if call is None:
            return
        preview = call.append_delta(event.delta.text)
        owner = self._activity_owner(event.step.run)
        if owner is not None:
            owner.set_activity(
                event.step.run,
                truncate(one_line(preview), 100) or "responding…",
            )
            return
        block = self._blocks.get(event.step)
        if isinstance(block, blocks.ModelStepBlock):
            block.update(event)

    def _end_step(self, event: StepEnd, app: AppContext) -> None:
        self._outcomes[event.step] = event
        run = self._runs.get(event.step.run)
        if run is not None:
            run.metrics.record_step(event)
        if state := self._statements.get(event.step):
            self._end_statement(state, event, app)
            return
        call = self._calls.get(event.step)
        if call is None:
            return
        call.finish(event)
        owner = self._activity_owner(event.step.run)
        if owner is not None:
            owner.set_activity(event.step.run, call.completed_label(event))
            return
        block = self._blocks.get(event.step)
        if block is None:
            return
        block.update(event)
        if event.status != "finished":
            self._assign_block_error(block, self._new_error(event.error))
            self._finalize(event.step, app)
        elif event.kind != "model":
            self._finalize(event.step, app)

    def _end_statement(
        self,
        state: StatementState,
        event: StepEnd,
        app: AppContext,
    ) -> None:
        state.finish(event)
        if state.live_owner is not None:
            owner = self._live_owner(state)
            owner.active_activity = (
                "iteration completed"
                if event.status == "finished"
                else f"iteration {status_label(event.status)}"
            )
            return
        block = self._blocks.get(event.step)
        if isinstance(block, blocks.FlowStepBlock):
            block.display_error = self._new_error(event.error)
        self._finalize(event.step, app)

    def _end_run(self, event: RunEnd, app: AppContext) -> None:
        run = self._runs.get(event.run)
        if run is None:
            return
        run.finish(event)
        if event.run == self._root_run_id:
            self._end_root(run, event, app)
            return
        owner = self._statements.get(run.parent) if run.parent is not None else None
        if owner is None:
            return
        owner.child_finished(run)
        if not owner.batched:
            block = self._blocks.get(owner.begin.step)
            if isinstance(block, blocks.FlowStepBlock):
                block.note_child_run(run)
        parent_run = self._runs.get(owner.begin.step.run)
        if parent_run is not None:
            parent_run.metrics.add(run.metrics)
        if self._is_until(run, owner) and run.status == "finished":
            owner.record_until_decision(self._until_decision(event))
        live_owner = self._live_owner(owner)
        if owner.live_owner is not None and live_owner.active_run == run.run_id:
            live_owner.active_run = None
            live_owner.active_activity = f"↳ {run.run_id} {status_label(run.status)}"
        self._discard_run_models(run.run_id, app)

    def _end_root(
        self,
        run: RunState,
        event: RunEnd,
        app: AppContext,
    ) -> None:
        output_step = (
            event.output.step if isinstance(event.output, StepOutputRef) else None
        )
        show_inline_result = run.kind != "flow"
        output_finalized = False
        for step, block in list(self._blocks.items()):
            if (
                show_inline_result
                and isinstance(block, blocks.ModelStepBlock)
                and step == output_step
            ):
                self._finalize(step, app)
                output_finalized = True
            elif isinstance(block, blocks.RunStopBlock):
                continue
            elif block in app.get_live_blocks():
                self._discard(block, app)
                self._blocks.pop(step, None)
        if show_inline_result and not output_finalized and output_step is not None:
            if outcome := self._outcomes.get(output_step):
                response = blocks.AssistantResponseBlock.create(outcome)
                if response.render() is not None:
                    self._insert_before_run(response, app)
                    app.finalize_block(response)
        self._finalize_commands(app, blocks.RunSteerBlock, event)
        stop = next(
            (
                block
                for block in app.get_live_blocks()
                if isinstance(block, blocks.RunStopBlock) and block.run_id == event.run
            ),
            None,
        )
        if stop is None:
            stop = blocks.RunStopBlock.create(event)
            self._append_tail(stop, app)
        else:
            stop.update(event)
        stop.set_metrics(run.metrics, include_child_runs=run.kind == "flow")
        if event.status != "finished":
            stop.error = self._new_error(event.error)
        if (
            run.kind == "flow"
            and event.status == "finished"
            and output_step is not None
        ):
            app.finalize_block(blocks.ResultAvailableBlock(event.run))
        app.finalize_block(stop)
        app.finish_run()
        self.reset()

    def _activity_owner(self, run_id: str) -> StatementState | None:
        run = self._runs.get(run_id)
        if run is None or run.parent is None:
            return None
        owner = self._statements.get(run.parent)
        return self._live_owner(owner) if owner is not None else None

    def _repeat_owner(self, run: RunState | None) -> StatementState | None:
        if run is None or run.parent is None:
            return None
        owner = self._statements.get(run.parent)
        if owner is None:
            return None
        live_owner = self._live_owner(owner)
        return live_owner if live_owner.statement == "repeat" else None

    def _direct_repeat_owner(self, step: StepPath) -> StatementState | None:
        parent = step.parent
        owner = self._statements.get(parent) if parent is not None else None
        return owner if owner is not None and owner.statement == "repeat" else None

    def _live_owner(self, statement: StatementState) -> StatementState:
        if statement.live_owner is None:
            return statement
        return self._statements.get(statement.live_owner, statement)

    def _until_decision(self, event: RunEnd) -> bool | None:
        if not isinstance(event.output, StepOutputRef):
            return None
        outcome = self._outcomes.get(event.output.step)
        value = output_preview(outcome).strip() if outcome is not None else ""
        if value == "true":
            return True
        if value == "false":
            return False
        return None

    @staticmethod
    def _is_until(run: RunState, owner: StatementState) -> bool:
        return owner.statement == "repeat" and run.placement.get("role") == "until"

    def _discard_pending_models(self, next_step: StepPath, app: AppContext) -> None:
        next_run = next_step.run
        for step, block in list(self._blocks.items()):
            if (
                step != next_step
                and step.run == next_run
                and isinstance(block, blocks.ModelStepBlock)
                and self._calls.get(step) is not None
                and self._calls[step].end is not None
            ):
                block.hide_internal_output()
                self._finalize(step, app)

    def _discard_run_models(self, run_id: str, app: AppContext) -> None:
        for step, block in list(self._blocks.items()):
            if step.run == run_id and isinstance(block, blocks.ModelStepBlock):
                self._discard(block, app)
                self._blocks.pop(step, None)

    def _new_error(self, error: str | None) -> str:
        value = friendly_error((error or "").strip())
        if not value or value in self._reported_errors:
            return ""
        self._reported_errors.add(value)
        return value

    @staticmethod
    def _assign_block_error(block: blocks.MutableBlock, error: str) -> None:
        if isinstance(
            block,
            (
                blocks.DefaultStepBlock,
                blocks.ModelStepBlock,
                blocks.ToolStepBlock,
            ),
        ):
            block.error = error

    def _finalize(self, step: StepPath, app: AppContext) -> None:
        block = self._blocks.pop(step, None)
        if block is not None:
            app.finalize_block(block)

    @staticmethod
    def _discard(block: blocks.MutableBlock, app: AppContext) -> None:
        live = app.get_live_blocks()
        live[:] = [item for item in live if item is not block]

    @staticmethod
    def _insert_before_run(block: blocks.MutableBlock, app: AppContext) -> None:
        live = app.get_live_blocks()
        for index in range(len(live) - 1, -1, -1):
            if isinstance(live[index], blocks.RunStopBlock):
                live.insert(index, block)
                return
        live.append(block)

    @staticmethod
    def _append_tail(block: blocks.MutableBlock, app: AppContext) -> None:
        app.get_live_blocks().append(block)

    @staticmethod
    def _call_block(event: StepBegin) -> blocks.MutableBlock:
        if event.kind == "model":
            return blocks.ModelStepBlock.create(event)
        if event.kind == "tool":
            return blocks.ToolStepBlock.create(event)
        return blocks.DefaultStepBlock.create(event)

    @staticmethod
    def _finalize_commands(
        app: AppContext,
        block_type: type[blocks.RunStartBlock] | type[blocks.RunSteerBlock],
        event: RunBegin | StepBegin | RunEnd,
    ) -> None:
        target_run = (
            event.run
            if isinstance(event, (RunBegin, RunEnd))
            else event.step.run
        )
        for block in list(app.get_live_blocks()):
            if not isinstance(block, block_type):
                continue
            if isinstance(block, blocks.RunSteerBlock) and block.run_id != target_run:
                continue
            if isinstance(block, blocks.RunSteerBlock):
                if isinstance(event, RunBegin):
                    continue
                block.update(event)
            else:
                block.update(event)
            app.finalize_block(block)
