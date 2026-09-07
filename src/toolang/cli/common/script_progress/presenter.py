"""Script sink for shared execution progress updates."""

from __future__ import annotations

import asyncio
import sys
from typing import TextIO

from toolang.execution.events import RunBegin, RunEnd, RunEvent, RunTracer, StepBegin

from ..execution_progress import ProgressProjector, ProgressBlock, ProgressUpdate
from ..execution_progress.step_projection import runtime_tool_name, trace_live_rows
from ..execution_progress.config import DEFAULT_MAX_PROGRESS_WIDTH
from .blocks import RunBlock
from .console import ProgressConsole


class ScriptRunPresenter(RunTracer):
    """Render one script Run with shared terminal-independent semantics."""

    def __init__(
        self,
        *,
        run_id: str | None,
        operation: str | None = None,
        stream: TextIO | None = None,
        width: int | None = None,
        max_width: int = DEFAULT_MAX_PROGRESS_WIDTH,
    ) -> None:
        self.run_id = run_id
        self.operation = operation
        self.console = ProgressConsole(
            stream or sys.stderr,
            width=width,
            max_width=max_width,
        )
        self._projector = ProgressProjector()
        self._root: RunBlock | None = None
        self._refresh_task: asyncio.Task[None] | None = None

    async def on_event(self, event: RunEvent) -> None:
        if isinstance(event, RunBegin) and event.parent is None:
            if self.run_id is None:
                self.run_id = event.run
            self._begin_root(event)

        self.console.apply(self._projector.handle(event))

        if (
            not self.console.tty
            and isinstance(event, StepBegin)
            and runtime_tool_name(event) == "compact"
        ):
            self.console.apply(
                ProgressUpdate(
                    committed=(
                        ProgressBlock(f"step:{event.step}", trace_live_rows(event, "")),
                    )
                )
            )
        if self.console.tty and self._projector.has_timed_activity:
            if self._refresh_task is None:
                self._refresh_task = asyncio.create_task(self._refresh())
        else:
            self._stop_refresh()

        if isinstance(event, RunEnd) and event.run == self.run_id:
            self._end_root(event)

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
            self.console.apply(self._projector.refresh())

    def _begin_root(self, event: RunBegin) -> None:
        root = RunBlock.from_event(event, operation=self.operation)
        self._root = root

    def _end_root(self, event: RunEnd) -> None:
        root = self._root
        if root is None:
            return
        root.metrics = self._projector.root_metrics
        root.render_result(
            self.console,
            event,
            gap_before=self._projector.needs_footer_gap,
        )
