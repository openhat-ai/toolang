"""Private run-event persistence for RunExecutor."""

from __future__ import annotations

from collections.abc import Mapping

from ..events import RunBegin, RunEnd, RunEvent, StepBegin, StepEnd
from ..records import model_call_from_data
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
        given = event.given
        if event.kind == "model" and "call" in given:
            raw_model = given.get("model")
            raw_call = given.get("call")
            if not isinstance(raw_model, Mapping) or not isinstance(raw_call, Mapping):
                raise ValueError("model step requires model and call objects")
            given = self._store.capture_model_call(
                target=raw_model,
                call=model_call_from_data(raw_call),
            )
        self._store.begin_step(
            path=event.step,
            kind=event.kind,
            input=event.input,
            given=given,
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
