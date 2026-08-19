"""Chat sink for shared execution progress updates."""

from __future__ import annotations

from toolang.execution.events import RunBegin, RunEnd, RunEvent, StepBegin

from toolang.cli.common.execution_progress import (
    ExecutionProgressReducer,
    ProgressUpdate,
)

from . import blocks
from .base import AppContext, friendly_error


class ChatRunPresenter:
    """Own chat block lifetime while sharing execution presentation semantics."""

    def __init__(self) -> None:
        self._root_run_id: str | None = None
        self._reducer = ExecutionProgressReducer()
        self._progress: dict[str, blocks.ExecutionProgressBlock] = {}

    def handle(self, event: RunEvent, app: AppContext) -> None:
        if isinstance(event, RunBegin) and event.parent is None:
            if not self._begin_root(event, app):
                return
        elif isinstance(event, StepBegin):
            self._finalize_commands(app, blocks.RunSteerBlock, event)

        self._apply(self._reducer.handle(event), app)

        if isinstance(event, RunEnd) and event.run == self._root_run_id:
            self._end_root(event, app)

    def handle_error(self, app: AppContext, message: str) -> bool:
        """Close an accepted or pending Run after a local execution error."""

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
        self._apply(self._reducer.diagnostic(friendly_error(message)), app)
        stop = self._run_stop(app, active_run_id or self._root_run_id or "run")
        if stop is None:
            stop = blocks.RunStopBlock(
                run_id=active_run_id or self._root_run_id or "run",
                status="failed",
            )
            self._append_tail(stop, app)
        else:
            stop.status = "failed"
            stop.error = ""
        stop.set_metrics(
            self._reducer.root_metrics,
            include_child_runs=self._reducer.root_kind == "flow",
        )
        app.finalize_block(stop)
        app.finish_run()
        self.reset()
        return True

    def reset(self) -> None:
        self._root_run_id = None
        self._reducer = ExecutionProgressReducer()
        self._progress.clear()

    def _begin_root(self, event: RunBegin, app: AppContext) -> bool:
        if app.get_active_run() not in {None, event.run}:
            return False
        self._root_run_id = event.run
        app.set_active_run(event.run)
        self._finalize_commands(app, blocks.RunStartBlock, event)
        self._append_tail(blocks.RunStopBlock.create(event), app)
        return True

    def _end_root(self, event: RunEnd, app: AppContext) -> None:
        self._finalize_commands(app, blocks.RunSteerBlock, event)
        stop = self._run_stop(app, event.run)
        if stop is None:
            stop = blocks.RunStopBlock.create(event)
            self._append_tail(stop, app)
        else:
            stop.update(event)
        stop.error = ""
        stop.set_metrics(
            self._reducer.root_metrics,
            include_child_runs=self._reducer.root_kind == "flow",
        )
        if self._reducer.root_kind == "flow" and event.status == "succeeded":
            app.finalize_block(blocks.ResultAvailableBlock(event.run))
        app.finalize_block(stop)
        app.finish_run()
        self.reset()

    def _apply(self, update: ProgressUpdate, app: AppContext) -> None:
        for progress in update.stable:
            block = self._progress.pop(progress.key, None)
            if block is None:
                block = blocks.ExecutionProgressBlock(progress)
            else:
                block.update(progress)
            app.finalize_block(block)

        desired: dict[str, blocks.ExecutionProgressBlock] = {}
        ordered: list[blocks.ExecutionProgressBlock] = []
        for progress in update.live:
            block = self._progress.get(progress.key)
            if block is None:
                block = blocks.ExecutionProgressBlock(progress)
            else:
                block.update(progress)
            desired[progress.key] = block
            ordered.append(block)

        live = app.get_live_blocks()
        live[:] = [
            block
            for block in live
            if not isinstance(block, blocks.ExecutionProgressBlock)
        ]
        for block in ordered:
            self._insert_before_run(block, app)
        self._progress = desired

    @staticmethod
    def _run_stop(app: AppContext, run_id: str) -> blocks.RunStopBlock | None:
        return next(
            (
                block
                for block in app.get_live_blocks()
                if isinstance(block, blocks.RunStopBlock) and block.run_id == run_id
            ),
            None,
        )

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
    def _finalize_commands(
        app: AppContext,
        block_type: type[blocks.RunStartBlock] | type[blocks.RunSteerBlock],
        event: RunBegin | StepBegin | RunEnd,
    ) -> None:
        target_run = (
            event.run if isinstance(event, (RunBegin, RunEnd)) else event.step.run
        )
        for block in list(app.get_live_blocks()):
            if not isinstance(block, block_type):
                continue
            if isinstance(block, blocks.RunSteerBlock):
                if block.run_id != target_run or isinstance(event, RunBegin):
                    continue
                block.update(event)
            else:
                block.update(event)
            app.finalize_block(block)
