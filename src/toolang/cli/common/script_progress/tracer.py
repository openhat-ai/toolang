"""Script sink for shared execution progress updates."""

from __future__ import annotations

from collections.abc import Mapping
import sys
from typing import TextIO

from toolang.base.types.message import Part
from toolang.execution.events import RunBegin, RunEnd, RunEvent, RunTracer, StepEnd
from toolang.execution.types import StepPath, TypedPointer

from ..execution_progress import ExecutionProgressReducer
from .blocks import RunBlock
from .console import ProgressConsole


class ConsoleRunTracer(RunTracer):
    """Render one script Run with shared terminal-independent semantics."""

    def __init__(
        self,
        *,
        run_id: str,
        verbosity: int = 0,
        stream: TextIO | None = None,
        runnable_kind: str | None = None,
        runnable_name: str | None = None,
        runnable_doc: str | None = None,
        input_value: tuple[Part, ...] = (),
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
        self._reducer = ExecutionProgressReducer()
        self._root: RunBlock | None = None

    async def on_event(self, event: RunEvent) -> None:
        if isinstance(event, RunBegin) and event.parent is None:
            self._begin_root(event)

        self.console.apply(self._reducer.handle(event))

        if isinstance(event, RunEnd) and event.run == self.run_id:
            self._end_root(event)

    def close(self) -> None:
        """Remove the bounded live area without changing stable scrollback."""

        self.console.close()

    def _begin_root(self, event: RunBegin) -> None:
        root = RunBlock.from_event(event)
        self._root = root
        root.render_header(
            self.console,
            verbosity=self.verbosity,
            kind=self.runnable_kind,
            name=self.runnable_name,
            doc=self.runnable_doc,
            input_value=self.input_value,
            args=self.args,
            control_index=event.control.index,
        )

    def _end_root(self, event: RunEnd) -> None:
        root = self._root
        if root is None:
            return
        root.finish(event)
        root.metrics = self._reducer.root_metrics
        root.render_result(
            self.console,
            event,
            output=self._output(event),
            error="",
        )

    def _output(self, event: RunEnd) -> StepEnd | None:
        if event.output is None or not isinstance(event.output.value, TypedPointer):
            return None
        try:
            step = StepPath.parse(event.output.value.pointer.anchor)
        except ValueError:
            return None
        return self._reducer.outcome(step)
