"""Private run-event persistence for RunExecutor."""

from __future__ import annotations

from dataclasses import replace

from toolang.lang.ast import AwaitStmt

from ..events import RunBegin, RunEnd, RunEvent, StepBegin, StepEnd
from ..store import RunStore
from ..types import ErrorMessage, ErrorRef, Output, ToolStepGiven, value_for_type


class _PersistSink:
    """Project ordered run events for one RunExecutor."""

    def __init__(self, store: RunStore) -> None:
        self._store = store

    def on_event(self, event: RunEvent) -> RunEvent:
        """Persist one run event in emission order."""

        if isinstance(event, RunBegin):
            self._store.begin_run(
                run_id=event.run,
                control=event.control,
                occurrence=event.occurrence,
                started_at=event.started_at,
            )
            return event
        if isinstance(event, StepBegin):
            self._begin_step(event)
            return event
        if isinstance(event, StepEnd):
            return self._finish_step(event)
        if isinstance(event, RunEnd):
            self._finish_run(event)
        return event

    def _begin_step(self, event: StepBegin) -> None:
        state = event.state
        if state is None:
            run = self._store.get_run(run_id=event.step.run_id)
            if run is None:
                raise ValueError(
                    f"step begin requires an Agent State reference: {event.step}"
                )
            state = run.state
        self._store.begin_step(
            preceded_by=event.preceded_by,
            ref=event.step,
            kind=event.kind,
            input=event.input,
            occurrence=event.occurrence,
            given=event.given,
            state=state,
            started_at=event.started_at,
        )

    def _finish_step(self, event: StepEnd) -> StepEnd:
        step = self._store.finish_step(
            aborted_by=event.aborted_by,
            ref=event.step,
            kind=event.kind,
            status=event.status,
            output=event.output,
            noted=event.noted,
            error=event.error,
            finished_at=event.finished_at,
        )
        output = step.output
        if isinstance(step.given, AwaitStmt) and output is not None:
            # Keep the durable target pointer; deliver the complete observed
            # value to callers that did not subscribe to the target's events.
            output = Output(
                value_for_type(output.type, self._store.resolve_value(output.value)),
                output.binding,
            )
        error = step.error
        if isinstance(error, ErrorRef) and (
            isinstance(step.given, AwaitStmt)
            or isinstance(step.given, ToolStepGiven)
            and step.given.call.name == "_toolang__await"
        ):
            error = ErrorMessage(self._store.resolve_error(error))
        return replace(
            event,
            status=step.status,
            output=output,
            noted=step.noted,
            error=error,
            aborted_by=step.aborted_by,
            finished_at=step.finished_at,
        )

    def _finish_run(self, event: RunEnd) -> None:
        self._store.finish_run(
            run_id=event.run,
            status=event.status,
            error=event.error,
            output=event.output,
            finished_at=event.finished_at,
        )
