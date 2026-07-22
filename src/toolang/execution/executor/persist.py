"""Mandatory run-event persistence."""

from __future__ import annotations

from collections.abc import Sequence
import threading

from toolang.base.types.message import TextPart

from ..events import RunBegin, RunEnd, RunEvent, StepBegin, StepEnd
from ..records import (
    OutputRef,
    RunControlRef,
    StepInputItem,
    trace_index,
    trace_parent,
    trace_run,
)
from ..store import RunStore
from ..types import StepPath

_DEFAULT_BINDING = object()


class PersistSink:
    """Project ordered run events into durable run and step records."""

    def __init__(self, store: RunStore) -> None:
        self._store = store
        self._lock = threading.Lock()
        self._last_step_index: dict[str, int] = {}
        self._failed_runs: set[str] = set()
        self._locals: dict[StepPath, dict[str, RunControlRef | OutputRef]] = {}
        self._bindings: dict[StepPath, str | None] = {}

    def on_event(self, event: RunEvent) -> None:
        """Persist one run event in emission order."""

        with self._lock:
            self._on_event(event)

    def _on_event(self, event: RunEvent) -> None:
        if isinstance(event, RunBegin):
            self._store.begin_run(
                run_id=event.run,
                context=event.context,
                started_at=event.started_at,
            )
            self._locals[event.run] = {"_": event.input}
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
        run_id = trace_run(event.step)
        parent = trace_parent(event.step)
        index = trace_index(event.step)
        if parent is None or index is None:
            raise ValueError(f"step_begin requires a step path: {event.step}")
        locals_ = self._locals.setdefault(parent, {})
        explicit = tuple(event.input)
        steer_inputs = [
            item
            for item in explicit
            if isinstance(item, RunControlRef) and item.index > 0
        ]
        if steer_inputs:
            locals_["_"] = steer_inputs[-1]
        reads = event.context.get("reads")
        inferred = (
            tuple(
                locals_[name]
                for name in reads
                if isinstance(name, str) and name in locals_
            )
            if isinstance(reads, Sequence) and not isinstance(reads, (str, bytes))
            else ()
        )
        self._store.begin_step(
            parent=parent,
            index=index,
            kind=event.kind,
            input=_unique_step_inputs((*explicit, *inferred)),
            context=event.context,
            started_at=event.started_at,
        )
        self._locals[event.step] = dict(locals_)
        raw_binding = event.context.get("binding", _DEFAULT_BINDING)
        self._bindings[event.step] = (
            "_"
            if raw_binding is _DEFAULT_BINDING and event.kind == "model"
            else raw_binding
            if isinstance(raw_binding, str)
            else None
        )
        if parent == run_id:
            self._last_step_index[run_id] = max(
                self._last_step_index.get(run_id, -1), index
            )

    def _finish_step(self, event: StepEnd) -> None:
        if event.status == "failed":
            self._failed_runs.add(trace_run(event.step))
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
        binding = self._bindings.pop(event.step, None)
        if event.status == "finished" and binding is not None:
            self._locals.setdefault(parent, {})[binding] = OutputRef(step=event.step)
        self._locals.pop(event.step, None)

    def _finish_run(self, event: RunEnd) -> None:
        if event.status == "failed" and event.run not in self._failed_runs:
            self._append_runtime_failure_step(event)
        projected_output = self._locals.get(event.run, {}).get("_")
        self._store.finish_run(
            run_id=event.run,
            status=event.status,
            error=event.error,
            output=(
                projected_output
                if isinstance(projected_output, OutputRef)
                else event.output
            ),
            finished_at=event.finished_at,
        )
        self._clear_run_state(event.run)

    def _clear_run_state(self, run_id: str) -> None:
        """Release transient projection state after one run ends."""

        self._last_step_index.pop(run_id, None)
        self._failed_runs.discard(run_id)
        for path in tuple(self._locals):
            if trace_run(path) == run_id:
                self._locals.pop(path, None)
        for path in tuple(self._bindings):
            if trace_run(path) == run_id:
                self._bindings.pop(path, None)

    def _append_runtime_failure_step(self, event: RunEnd) -> None:
        step_index = self._last_step_index.get(event.run, -1) + 1
        self._store.begin_step(
            parent=event.run,
            index=step_index,
            kind="system",
            input=(),
            context={},
            started_at=event.finished_at,
        )
        self._store.finish_step(
            parent=event.run,
            index=step_index,
            kind="system",
            status="failed",
            output=(TextPart(text=event.error or "Run failed."),),
            detail={},
            error=event.error,
            finished_at=event.finished_at,
        )
        self._last_step_index[event.run] = step_index


def _unique_step_inputs(items: Sequence[StepInputItem]) -> tuple[StepInputItem, ...]:
    result: list[StepInputItem] = []
    for item in items:
        if item not in result:
            result.append(item)
    return tuple(result)
