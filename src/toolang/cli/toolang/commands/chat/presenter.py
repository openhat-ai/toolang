"""Chat sink for shared execution progress updates."""

from __future__ import annotations

from toolang.execution.events import RunBegin, RunEnd, RunEvent, StepBegin
from toolang.execution.schemas import RunDetail

from toolang.cli.common.execution_progress import (
    ProgressProjector,
    ProgressUpdate,
)
from toolang.cli.common.execution_progress.config import DEFAULT_MAX_PROGRESS_WIDTH
from toolang.cli.common.terminal_surfaces import DARK_TERMINAL_SURFACES

from . import blocks
from .base import AppContext, friendly_error


class ChatRunPresenter:
    """Own chat block lifetime while sharing execution presentation semantics."""

    def __init__(
        self,
        *,
        max_width: int = DEFAULT_MAX_PROGRESS_WIDTH,
        code_background: str = DARK_TERMINAL_SURFACES.code_background,
    ) -> None:
        self._root_run_id: str | None = None
        self._projector = ProgressProjector()
        self._progress: dict[str, blocks.ExecutionProgressBlock] = {}
        self._max_width = max_width
        self._code_background = code_background

    def handle(self, event: RunEvent, app: AppContext) -> None:
        if isinstance(event, RunBegin) and event.parent is None:
            if not self._begin_root(event, app):
                return
        elif isinstance(event, StepBegin):
            self._finalize_commands(app, blocks.RunSteerBlock, event)

        self._apply(self._projector.handle(event), app)

        if isinstance(event, RunEnd) and event.run == self._root_run_id:
            self._end_root(event, app)

    def handle_error(self, app: AppContext, message: str) -> bool:
        """Close an accepted or pending Run after a local execution error."""

        active_run_id = app.get_active_run()
        if active_run_id is None and not app.get_live_blocks():
            return False
        if active_run_id is None and self._root_run_id is None:
            for block in list(app.get_live_blocks()):
                if isinstance(block, blocks.RunControlBlock):
                    app.finalize_block(block)
                else:
                    self._discard(block, app)
            app.finalize_block(blocks.SubmissionErrorBlock(message))
            self.reset()
            app.finish_run()
            return True

        for block in list(app.get_live_blocks()):
            if isinstance(block, (blocks.RunControlBlock, blocks.RunSteerBlock)):
                app.finalize_block(block)
        self._apply(self._projector.diagnostic(friendly_error(message)), app)
        summary = self._run_summary(app, active_run_id or self._root_run_id or "run")
        if summary is None:
            summary = blocks.RunSummaryBlock(
                run_id=active_run_id or self._root_run_id or "run",
                status="failed",
                max_width=self._max_width,
            )
            self._append_tail(summary, app)
        else:
            summary.status = "failed"
            summary.error = ""
        summary.set_metrics(self._projector.root_metrics)
        summary.gap_before = self._projector.needs_footer_gap
        app.finalize_block(summary)
        app.finish_run()
        self.reset()
        return True

    def handle_recovered(self, app: AppContext, detail: RunDetail) -> bool:
        """Finalize an incomplete stream from durable terminal run truth."""

        if detail.status in {"pending", "running"}:
            return False
        active_run_id = app.get_active_run()
        if active_run_id not in {None, detail.id}:
            return False
        for block in list(app.get_live_blocks()):
            if isinstance(block, (blocks.RunControlBlock, blocks.RunSteerBlock)):
                app.finalize_block(block)
        self._apply(
            self._projector.diagnostic(
                "Live output may be incomplete after reconnecting; inspect the "
                f"durable result with /output {detail.id}."
            ),
            app,
        )
        summary = self._run_summary(app, detail.id)
        if summary is None:
            summary = blocks.RunSummaryBlock(
                run_id=detail.id,
                status=detail.status,
                started_at=detail.started_at,
                finished_at=detail.finished_at or "",
                max_width=self._max_width,
            )
            self._append_tail(summary, app)
        else:
            summary.status = detail.status
            summary.finished_at = detail.finished_at or summary.finished_at
        summary.error = friendly_error(detail.error) if detail.error else ""
        summary.set_metrics(self._projector.root_metrics)
        summary.gap_before = self._projector.needs_footer_gap
        app.finalize_block(summary)
        app.finish_run()
        self.reset()
        return True

    def reset(self) -> None:
        self._root_run_id = None
        self._projector = ProgressProjector()
        self._progress.clear()

    def _begin_root(self, event: RunBegin, app: AppContext) -> bool:
        if app.get_active_run() not in {None, event.run}:
            return False
        self._root_run_id = event.run
        app.set_active_run(event.run)
        self._finalize_commands(app, blocks.RunControlBlock, event)
        self._append_tail(
            blocks.RunSummaryBlock.create(event, max_width=self._max_width),
            app,
        )
        return True

    def _end_root(self, event: RunEnd, app: AppContext) -> None:
        self._finalize_commands(app, blocks.RunSteerBlock, event)
        summary = self._run_summary(app, event.run)
        if summary is None:
            summary = blocks.RunSummaryBlock.create(event, max_width=self._max_width)
            self._append_tail(summary, app)
        else:
            summary.update(event)
        summary.error = ""
        summary.set_metrics(self._projector.root_metrics)
        summary.gap_before = self._projector.needs_footer_gap
        app.finalize_block(summary)
        app.finish_run()
        self.reset()

    def _apply(
        self,
        update: ProgressUpdate,
        app: AppContext,
    ) -> None:
        for projected in update.committed:
            progress = projected
            block = self._progress.pop(progress.key, None)
            if block is None:
                block = blocks.ExecutionProgressBlock(
                    progress,
                    max_width=self._max_width,
                    code_background=self._code_background,
                )
            else:
                block.update(progress)
            block.live = False
            app.finalize_block(block)

        desired: dict[str, blocks.ExecutionProgressBlock] = {}
        ordered: list[blocks.ExecutionProgressBlock] = []
        for progress in update.live:
            block = self._progress.get(progress.key)
            if block is None:
                block = blocks.ExecutionProgressBlock(
                    progress,
                    live=True,
                    max_width=self._max_width,
                    code_background=self._code_background,
                )
            else:
                block.update(progress)
                block.live = True
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
    def _run_summary(app: AppContext, run_id: str) -> blocks.RunSummaryBlock | None:
        return next(
            (
                block
                for block in app.get_live_blocks()
                if isinstance(block, blocks.RunSummaryBlock) and block.run_id == run_id
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
            if isinstance(live[index], blocks.RunSummaryBlock):
                live.insert(index, block)
                return
        live.append(block)

    @staticmethod
    def _append_tail(block: blocks.MutableBlock, app: AppContext) -> None:
        app.get_live_blocks().append(block)

    @staticmethod
    def _finalize_commands(
        app: AppContext,
        block_type: type[blocks.RunControlBlock] | type[blocks.RunSteerBlock],
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
