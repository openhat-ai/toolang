"""Private run-event persistence for RunExecutor."""

from __future__ import annotations

from ..events import RunBegin, RunEnd, RunEvent, StepBegin, StepEnd
from ..store import RunStore


class _PersistSink:
    """Project ordered run events for one RunExecutor."""

    def __init__(self, store: RunStore) -> None:
        self._store = store

    def on_event(self, event: RunEvent) -> None:
        """Persist one run event in emission order."""

        if isinstance(event, RunBegin):
            self._store.begin_run(
                run_id=event.run,
                control=event.control,
                occurrence=event.occurrence,
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
        self._store.begin_step(
            path=event.step,
            kind=event.kind,
            input=event.input,
            occurrence=event.occurrence,
            given=event.given,
            started_at=event.started_at,
        )

    def _finish_step(self, event: StepEnd) -> None:
        self._store.finish_step(
            path=event.step,
            kind=event.kind,
            status=event.status,
            output=event.output,
            noted=event.noted,
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
