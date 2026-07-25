"""Local execution resources owned by one CLI command."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import sys
from typing import TextIO
from typing import Iterator

import click
import typer

from toolang.common.ids import IdIssuer
from toolang.execution.events import (
    RunBegin,
    RunEnd,
    RunEvent,
    RunTracer,
    StepBegin,
    StepEnd,
)
from toolang.execution.records import trace_run
from toolang.execution.store import RunStore

from .context import context_layout


@dataclass(frozen=True, slots=True)
class ExecutionResources:
    """Process-local access to one agent's durable execution state."""

    store: RunStore
    ids: IdIssuer


class ConsoleRunTracer(RunTracer):
    """Render concise ordered run progress to a text stream."""

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

    async def on_event(self, event: RunEvent) -> None:
        if isinstance(event, RunBegin):
            if event.run == self.run_id and self.verbosity:
                self._write(f"run {event.run} started")
            return
        if isinstance(event, StepBegin):
            if trace_run(event.step) == self.run_id:
                detail = f" {event.step}" if self.verbosity else ""
                self._write(f"→ {event.kind}{detail}")
            return
        if isinstance(event, StepEnd):
            if trace_run(event.step) != self.run_id:
                return
            if event.status == "finished" and not self.verbosity:
                return
            marker = "✓" if event.status == "finished" else "!"
            suffix = f": {event.error}" if event.error else ""
            self._write(f"{marker} {event.kind} {event.status}{suffix}")
            return
        if isinstance(event, RunEnd) and event.run == self.run_id:
            if self.verbosity or event.status != "finished":
                suffix = f": {event.error}" if event.error else ""
                self._write(f"run {event.status}{suffix}")

    def _write(self, value: str) -> None:
        print(value, file=self.stream, flush=True)


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
