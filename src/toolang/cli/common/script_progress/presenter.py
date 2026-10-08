"""Script sink for shared execution progress updates."""

from __future__ import annotations

import asyncio
import sys
from dataclasses import replace
from typing import TextIO

from toolang.execution.events import RunBegin, RunEnd, RunEvent, RunTracer, StepBegin

from ..terminal_surfaces import DARK_TERMINAL_SURFACES, TerminalSurfaces
from ..execution_progress import ProgressProjector, ProgressBlock, ProgressUpdate
from ..execution_progress.operations import normalize_operation
from ..execution_progress.step_projection import trace_live_rows
from ..execution_progress.config import DEFAULT_MAX_PROGRESS_WIDTH
from .blocks import RunBlock, RunContext
from .console import ProgressConsole


class ScriptRunPresenter(RunTracer):
    """Render one script Run with shared terminal-independent semantics."""

    def __init__(
        self,
        *,
        run_id: str | None,
        operation: str | None = None,
        context: RunContext | None = None,
        stream: TextIO | None = None,
        width: int | None = None,
        max_width: int = DEFAULT_MAX_PROGRESS_WIDTH,
        surfaces: TerminalSurfaces = DARK_TERMINAL_SURFACES,
    ) -> None:
        self.run_id = run_id
        self.operation = operation
        self._context = context
        self._context_gap_pending = False
        self.console = ProgressConsole(
            stream or sys.stderr,
            width=width,
            max_width=max_width,
            surfaces=surfaces,
        )
        self._projector = ProgressProjector()
        self._root: RunBlock | None = None
        self._root_identity: tuple[str, str] | None = None
        self._completed: tuple[str, str] | None = None
        self._refresh_task: asyncio.Task[None] | None = None

    async def on_event(self, event: RunEvent) -> None:
        if isinstance(event, RunBegin) and event.parent is None:
            if self.run_id is None:
                self.run_id = event.run
            self._begin_root(event)

        update = self._projector.handle(event)
        if (
            not self.console.tty
            and isinstance(event, StepBegin)
            and (operation := normalize_operation(event)).timed
        ):
            update = replace(
                update,
                committed=(
                    *update.committed,
                    ProgressBlock(f"step:{event.step}", trace_live_rows(operation, "")),
                ),
            )
        self._apply_progress(update)
        self._sync_refresh()

        if isinstance(event, RunEnd) and event.run == self.run_id:
            self._end_root(event)

    async def on_snapshot(self, events: tuple[RunEvent, ...]) -> None:
        for event in events:
            if isinstance(event, RunBegin) and event.parent is None:
                if (event.run, str(event.control)) == self._completed:
                    return
                self.run_id = event.run
                self._begin_root(event)
        self._apply_progress(self._projector.restore(events))
        self._sync_refresh()
        for event in events:
            if isinstance(event, RunEnd) and event.run == self.run_id:
                self._end_root(event)

    def _sync_refresh(self) -> None:
        if self.console.tty and self._projector.has_timed_activity:
            if self._refresh_task is None:
                self._refresh_task = asyncio.create_task(self._refresh())
        else:
            self._stop_refresh()

    def _apply_progress(self, update: ProgressUpdate) -> None:
        """Apply progress with one header gap before the first committed output."""

        if self._context_gap_pending:
            for index, block in enumerate(update.committed):
                if not block.rows:
                    continue
                update = replace(
                    update,
                    committed=(
                        *update.committed[:index],
                        replace(block, gap_before=True),
                        *update.committed[index + 1 :],
                    ),
                )
                self._context_gap_pending = False
                break
        self.console.apply(update)

    def close(self) -> None:
        """Remove the bounded live area without changing committed scrollback."""

        self._stop_refresh()
        self.console.close()

    def _stop_refresh(self) -> None:
        if self._refresh_task is not None:
            self._refresh_task.cancel()
            self._refresh_task = None

    async def _refresh(self) -> None:
        while True:
            await asyncio.sleep(1)
            self._apply_progress(self._projector.refresh())

    def _begin_root(self, event: RunBegin) -> None:
        self._root_identity = (event.run, str(event.control))
        if self._root is not None:
            self._root.started_at = event.started_at
            return
        root = RunBlock.from_event(event, operation=self.operation)
        self._root = root
        if self._context is not None:
            self.console.write_renderable(
                replace(
                    self._context,
                    runnable=event.runnable or self._context.runnable,
                )
            )
            self._context_gap_pending = True

    def _end_root(self, event: RunEnd) -> None:
        if self._root_identity == self._completed:
            return
        self._completed = self._root_identity
        root = self._root
        if root is None:
            return
        root.metrics = self._projector.root_metrics
        root.render_result(
            self.console,
            event,
            gap_before=self._projector.needs_footer_gap,
        )
