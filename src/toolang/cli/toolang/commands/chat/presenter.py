"""Chat sink for shared execution progress updates."""

from __future__ import annotations

from dataclasses import dataclass

from toolang.execution.events import RunBegin, RunEnd, RunEvent, StepBegin, StepEnd
from toolang.execution.schemas import ControlInfo, RunDetail
from toolang.execution.types import StepRef

from toolang.cli.common.execution_progress import (
    ProgressProjector,
    ProgressUpdate,
)
from toolang.cli.common.execution_progress.config import DEFAULT_MAX_PROGRESS_WIDTH
from toolang.cli.common.terminal_surfaces import DARK_TERMINAL_SURFACES

from . import blocks
from .base import AppContext, SteerError, SteerReceipt, friendly_error


@dataclass(slots=True)
class _SteerSubmission:
    block: blocks.RunSteerBlock
    control: ControlInfo | None = None


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
        self._steers: dict[str, _SteerSubmission] = {}
        self._consumed: set[tuple[str, int]] = set()
        self._active_steps: set[StepRef] = set()
        self._feedback = blocks.SteerFeedbackBlock(max_width=max_width)
        self._stream_complete = True

    def handle(self, event: RunEvent, app: AppContext) -> None:
        if isinstance(event, RunBegin) and event.parent is None:
            if not self._begin_root(event, app):
                return
        elif isinstance(event, StepBegin):
            self._consumed.update(
                (str(ref.target), ref.index) for ref in event.preceded_by
            )
            if event.step.run_id == app.get_active_run():
                self._active_steps.add(event.step)
        elif isinstance(event, StepEnd):
            self._active_steps.discard(event.step)

        self._apply(self._projector.handle(event), app)
        self._adopt_steers(app)

        if isinstance(event, RunEnd) and event.run == self._root_run_id:
            self._end_root(event, app)

    def add_steer(
        self, submission_id: str, block: blocks.RunSteerBlock, app: AppContext
    ) -> None:
        """Register authored order before starting the transport request."""

        self._steers[submission_id] = _SteerSubmission(block)
        self._discard(self._feedback, app)
        app.get_live_blocks().append(block)
        self._refresh_steer_feedback(app)

    def refresh(self, app: AppContext) -> None:
        """Reuse the Chat ticker for active compaction progress."""

        if self._projector.has_timed_activity:
            self._apply(self._projector.refresh(), app)

    def handle_steer_receipt(self, receipt: SteerReceipt, app: AppContext) -> None:
        submission = self._steers.get(receipt.submission_id)
        if (
            submission is None
            or submission.block.run_id != receipt.run_id
            or receipt.control.run_id != receipt.run_id
        ):
            return
        submission.control = receipt.control
        self._adopt_steers(app)

    def handle_steer_error(self, error: SteerError, app: AppContext) -> bool:
        submission = self._steers.get(error.submission_id)
        if submission is None or submission.block.run_id != error.run_id:
            return False
        del self._steers[error.submission_id]
        app.finalize_block(submission.block)
        app.finalize_block(
            blocks.SubmissionErrorBlock(
                f"Could not steer the run: {friendly_error(error.message)}"
            )
        )
        self._adopt_steers(app)
        return True

    def mark_disconnected(self) -> None:
        self._stream_complete = False

    def _is_adopted(self, control: ControlInfo) -> bool:
        return (
            control.status == "applied"
            or (control.run_id, control.index) in self._consumed
        )

    def _adopt_steers(self, app: AppContext) -> None:
        for key, submission in list(self._steers.items()):
            control = submission.control
            if control is None:
                # An earlier outstanding receipt may belong to the same
                # consuming step. Keep that batch in authored order.
                break
            if self._is_adopted(control):
                app.finalize_block(submission.block)
                del self._steers[key]
        self._refresh_steer_feedback(app)

    def _refresh_steer_feedback(self, app: AppContext) -> None:
        self._discard(self._feedback, app)
        self._feedback.sending = sum(s.control is None for s in self._steers.values())
        self._feedback.accepted = sum(
            s.control is not None
            and s.control.status == "pending"
            and not self._is_adopted(s.control)
            for s in self._steers.values()
        )
        self._feedback.active_step = bool(self._active_steps)
        if self._feedback.sending or self._feedback.accepted:
            app.get_live_blocks().append(self._feedback)

    def _finish_steers(
        self,
        app: AppContext,
        *,
        detail: RunDetail | None = None,
        complete: bool = False,
    ) -> None:
        self._discard(self._feedback, app)
        controls = {c.index: c for c in detail.controls} if detail else {}
        consumed = set(self._consumed)
        if detail:
            consumed.update(
                (str(ref.target), ref.index)
                for step in detail.steps
                for ref in step.preceded_by
            )
        for submission in self._steers.values():
            receipt = submission.control
            control = controls.get(receipt.index, receipt) if receipt else None
            if control is not None:
                applied = (
                    control.status == "applied"
                    or (control.run_id, control.index) in consumed
                )
                submission.block.not_applied = not applied and (
                    control.status in {"wontapply", "revoked"}
                    or complete
                    or (detail is not None and control.index in controls)
                )
            app.finalize_block(submission.block)
        self._steers.clear()

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

        self._finish_steers(app)
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
        self.reset()
        app.finish_run()
        return True

    def handle_recovered(self, app: AppContext, detail: RunDetail) -> bool:
        """Finalize an incomplete stream from durable terminal run truth."""

        if detail.status in {"pending", "running"}:
            return False
        active_run_id = app.get_active_run()
        if active_run_id not in {None, detail.id}:
            return False
        self._finish_steers(app, detail=detail)
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
        self.reset()
        app.finish_run()
        return True

    def reset(self) -> None:
        self._root_run_id = None
        self._projector = ProgressProjector()
        self._progress.clear()
        self._steers.clear()
        self._consumed.clear()
        self._active_steps.clear()
        self._stream_complete = True

    def _begin_root(self, event: RunBegin, app: AppContext) -> bool:
        if app.get_active_run() not in {None, event.run}:
            return False
        self._root_run_id = event.run
        app.set_active_run(event.run)
        self._finalize_root_control(app, event)
        self._append_tail(
            blocks.RunSummaryBlock.create(event, max_width=self._max_width),
            app,
        )
        return True

    def _end_root(self, event: RunEnd, app: AppContext) -> None:
        self._finish_steers(app, complete=self._stream_complete)
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
        self.reset()
        app.finish_run()

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
        for index, item in enumerate(live):
            if isinstance(
                item,
                (
                    blocks.RunSummaryBlock,
                    blocks.RunSteerBlock,
                    blocks.SteerFeedbackBlock,
                ),
            ):
                live.insert(index, block)
                return
        live.append(block)

    @staticmethod
    def _append_tail(block: blocks.MutableBlock, app: AppContext) -> None:
        app.get_live_blocks().append(block)

    @staticmethod
    def _finalize_root_control(app: AppContext, event: RunBegin) -> None:
        for block in list(app.get_live_blocks()):
            if isinstance(block, blocks.RunControlBlock):
                block.update(event)
                app.finalize_block(block)
