"""Mandatory run-event persistence."""

from __future__ import annotations

from ..events import RunBegin, RunEnd, RunEvent, StepBegin, StepEnd
from ..records import trace_index, trace_parent
from ..store import RunStore


class PersistSink:
    """Project ordered run events into durable run and step records."""

    def __init__(self, store: RunStore) -> None:
        self._store = store

    def on_event(self, event: RunEvent) -> None:
        """Persist one run event in emission order."""

        if isinstance(event, RunBegin):
            self._store.begin_run(
                run_id=event.run,
                context=event.context,
                started_at=event.started_at,
            )
            return
        if isinstance(event, StepBegin):
            self._begin_step(event)
            return
        if isinstance(event, StepEnd):
            self._finish_step(event)
            return
        if isinstance(event, RunEnd):
            self._finish_run(event)

    def _begin_step(self, event: StepBegin) -> None:
        parent = trace_parent(event.step)
        index = trace_index(event.step)
        if parent is None or index is None:
            raise ValueError(f"step_begin requires a step path: {event.step}")
        self._store.begin_step(
            parent=parent,
            index=index,
            kind=event.kind,
            input=event.input,
            context=event.context,
            started_at=event.started_at,
        )

    def _finish_step(self, event: StepEnd) -> None:
        parent = trace_parent(event.step)
        index = trace_index(event.step)
        if parent is None or index is None:
            raise ValueError(f"step_end requires a step path: {event.step}")
        self._store.finish_step(
            parent=parent,
            index=index,
            kind=event.kind,
            status=event.status,
            output=event.output,
            detail=event.detail,
            error=event.error,
            finished_at=event.finished_at,
        )

    def _finish_run(self, event: RunEnd) -> None:
        self._store.finish_run(
            run_id=event.run,
            status=event.status,
            error=event.error,
            output=event.output,
            finished_at=event.finished_at,
        )
