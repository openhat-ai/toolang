"""Trace-event builders for execution persistence tests."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, cast

from toolang.base.types.message import Message, Part
from toolang.execution.events import (
    RunBegin,
    RunEnd,
    RunEvent,
    StepBegin,
    StepEnd,
)
from toolang.execution.records import (
    OutputRef,
    RunControlRecord,
    RunControlRef,
    RunRecord,
    StepInputItem,
    StepRecord,
    trace_child_path,
    trace_run,
)
from toolang.common.time import utc_now
from toolang.execution.executor.persist import PersistSink
from toolang.execution.store import RunStore
from toolang.execution.types import (
    ControlTiming,
    RunControlKind,
    RunStatus,
    StepKind,
    StepStatus,
)


def emit_event(store: RunStore, event: RunEvent) -> None:
    """Persist one trace event through the store's canonical sink."""

    sink = getattr(store, "_test_persist_sink", None)
    if sink is None:
        sink = PersistSink(store)
        setattr(store, "_test_persist_sink", sink)
    cast(PersistSink, sink).on_event(event)


def project_run_start(
    store: RunStore,
    *,
    run_id: str,
    thread_id: str,
    origin: str,
    input: Message,
    root_run_id: str | None = None,
    executable_kind: str = "agic",
    executable_name: str | None = None,
    call_kind: str = "top",
    metadata: Mapping[str, Any] | None = None,
    request_id: str | None = None,
    created_at: str | None = None,
    started_at: str | None = None,
    parent: str | None = None,
    context: Mapping[str, Any] | None = None,
) -> RunRecord:
    """Project accepted and begun run events, returning durable run truth."""

    created = created_at or utc_now()
    started = started_at or created
    run_context = dict(context or metadata or {})
    run_context.setdefault("origin", origin)
    run_context.setdefault("root", root_run_id or run_id)
    run_context.setdefault("runnable", {"kind": executable_kind, "name": executable_name})
    run_context.setdefault("call", call_kind)
    if store.get_thread(thread_id=thread_id) is None:
        store.create_thread(
            thread_id=thread_id,
            origin=origin,
            created_at=created,
        )
    store.accept_start(
        run_id=run_id,
        parent=parent,
        thread=thread_id,
        input=input,
        context=run_context,
        request_id=request_id,
        created_at=created,
    )
    emit_event(
        store,
        RunBegin(
            run=run_id,
            input=RunControlRef(index=0),
            context=run_context,
            started_at=started,
        ),
    )
    store.finish_run_controls(run_id=run_id, indexes=(0,), finished_at=started)
    run = store.get_run(run_id=run_id)
    if run is None:
        raise AssertionError(f"run was not projected: {run_id}")
    return run


def project_command(
    store: RunStore,
    *,
    run_id: str,
    kind: RunControlKind,
    timing: ControlTiming = "immediate",
    input: Message | None = None,
    context: Mapping[str, Any] | None = None,
    request_id: str | None = None,
    created_at: str | None = None,
) -> RunControlRecord:
    """Project one accepted steer or stop command event."""

    if kind == "start":
        raise ValueError("start controls are created by project_run_start")
    if kind == "steer" and input is None:
        raise ValueError("steer control requires input")
    return store.accept_run_control(
        run_id=run_id,
        kind=kind,
        timing=timing,
        input=input,
        context=dict(context or {}),
        request_id=request_id,
        created_at=created_at or utc_now(),
    )


def project_step(
    store: RunStore,
    *,
    run_id: str | None = None,
    step_index: int | None = None,
    parent: str | None = None,
    index: int | None = None,
    kind: StepKind,
    status: StepStatus,
    input: Sequence[StepInputItem],
    output: Sequence[Part],
    detail: Mapping[str, Any] | None = None,
    error: str | None = None,
    started_at: str,
    finished_at: str | None,
    context: Mapping[str, Any] | None = None,
) -> StepRecord:
    """Project step begin and, when terminal, step end events."""

    step_parent = parent or run_id
    resolved_index = index if index is not None else step_index
    if step_parent is None or resolved_index is None:
        raise ValueError("project_step requires parent/index or run_id/step_index")
    path = trace_child_path(step_parent, resolved_index)
    emit_event(
        store,
        StepBegin(
            step=path,
            kind=kind,
            input=tuple(input),
            given=dict(context or {}),
            started_at=started_at,
        ),
    )
    if status != "running":
        emit_event(
            store,
            StepEnd(
                step=path,
                kind=kind,
                status=status,
                output=tuple(output),
                noted=dict(detail or {}),
                error=error,
                finished_at=finished_at or started_at,
            ),
        )
    step = next(
        (item for item in store.list_steps(run_id=trace_run(path)) if item.path == path),
        None,
    )
    if step is None:
        raise AssertionError(f"step was not projected: {path}")
    return step


def project_run_end(
    store: RunStore,
    *,
    run_id: str,
    status: RunStatus = "finished",
    error: str | None = None,
    finished_at: str | None = None,
    output: OutputRef | None = None,
) -> RunRecord:
    """Project one terminal run event, returning durable run truth."""

    emit_event(
        store,
        RunEnd(
            run=run_id,
            status=status,
            output=output,
            error=error,
            finished_at=finished_at or utc_now(),
        ),
    )
    run = store.get_run(run_id=run_id)
    if run is None:
        raise AssertionError(f"run was not projected: {run_id}")
    return run
