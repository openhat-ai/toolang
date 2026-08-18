"""Build durable execution fixtures without running the executor."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Literal, cast

from toolang.base.types.message import Message, Part
from toolang.base.types.policy import RunBindings, RunLimits
from toolang.execution.events import (
    RunBegin,
    RunEnd,
    RunEvent,
    StepBegin,
    StepEnd,
)
from toolang.execution.records import (
    RunControlRecord,
    RunRecord,
    StepRecord,
)
from toolang.common.time import utc_now
from toolang.execution.executor._persist import _PersistSink
from toolang.execution.store import RunStore
from toolang.execution.types import (
    AgentResources,
    ControlRef,
    ControlTiming,
    ControlKind,
    RunStatus,
    StepKind,
    StepStatus,
    StepPath,
    Local,
    Pointer,
)
from toolang.lang.input import RunnableInput
from toolang.lang.types import Array


def persist_event(store: RunStore, event: RunEvent) -> None:
    """Persist one trace event through the executor's internal projector."""

    sink = getattr(store, "_test_event_projector", None)
    if sink is None:
        sink = _PersistSink(store)
        setattr(store, "_test_event_projector", sink)
    cast(_PersistSink, sink).on_event(event)


def accept_run_start(
    store: RunStore,
    *,
    run_id: str,
    parent: StepPath | None,
    thread: str,
    input: Message | RunnableInput,
    context: Mapping[str, Any],
    request_id: str | None,
    created_at: str,
    kind: Literal["start", "rerun"] = "start",
    source: str | None = None,
    bindings: RunBindings | None = None,
    limits: RunLimits | None = None,
    resources: AgentResources | None = None,
) -> tuple[RunRecord, RunControlRecord]:
    """Accept a run with explicit default preparation snapshots for store tests."""

    resolved_input = (
        input
        if isinstance(input, RunnableInput)
        else RunnableInput(primary=Array("Part[]", input.parts))
    )
    resolved_bindings = (
        bindings
        if bindings is not None
        else RunBindings(runnable="agic:test", model="test")
    )
    locals_value = (
        [Local.typed("Part[]", resolved_input.primary, "_", 0)]
        if resolved_input.primary is not None
        else []
    )
    locals_value.extend(
        Local.typed(
            "Json",
            value,
            name,
            0,
        )
        for name, value in resolved_input.named.items()
    )

    return store.accept_start(
        run_id=run_id,
        parent=parent,
        thread=thread,
        resources=resources if resources is not None else AgentResources(),
        limits=limits if limits is not None else RunLimits(),
        runnable=resolved_bindings.runnable or "agic:test",
        model=resolved_bindings.model or "test",
        locals=tuple(locals_value),
        placement=(
            dict(value)
            if isinstance((value := context.get("placement")), Mapping)
            else None
        ),
        request_id=request_id,
        created_at=created_at,
        kind=kind,
        source=source,
    )


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
    parent: StepPath | str | None = None,
    context: Mapping[str, Any] | None = None,
) -> RunRecord:
    """Project accepted and begun run events, returning durable run truth."""

    created = created_at or utc_now()
    started = started_at or created
    run_context = dict(context or metadata or {})
    del root_run_id
    del call_kind
    if store.get_thread(thread_id=thread_id) is None:
        store.create_thread(
            thread_id=thread_id,
            origin=origin,
            created_at=created,
        )
    parent_path = StepPath.parse(parent) if parent is not None else None
    store.accept_start(
        run_id=run_id,
        parent=parent_path,
        thread=thread_id,
        resources=AgentResources(),
        limits=RunLimits(),
        runnable=(
            f"{executable_kind}:{executable_name}"
            if executable_name is not None
            else f"{executable_kind}:test"
        ),
        model="test",
        locals=(Local.typed("Part[]", tuple(input.parts), "_", 0),),
        placement=(
            dict(value)
            if isinstance((value := run_context.get("placement")), Mapping)
            else None
        ),
        request_id=request_id,
        created_at=created,
    )
    persist_event(
        store,
        RunBegin(
            run=run_id,
            parent=parent_path,
            control=ControlRef(run_id, 0),
            runnable=(
                f"{executable_kind}:{executable_name}"
                if executable_name is not None
                else f"{executable_kind}:test"
            ),
            placement=(
                dict(value)
                if isinstance((value := run_context.get("placement")), Mapping)
                else None
            ),
            started_at=started,
        ),
    )
    store.finish_run_controls(run_id=run_id, indexes=(0,), finished_at=started)
    run = store.get_run(run_id=run_id)
    if run is None:
        raise AssertionError(f"run was not projected: {run_id}")
    return run


def project_run_control(
    store: RunStore,
    *,
    run_id: str,
    kind: ControlKind,
    timing: ControlTiming = "immediate",
    input: Message | None = None,
    context: Mapping[str, Any] | None = None,
    request_id: str | None = None,
    created_at: str | None = None,
) -> RunControlRecord:
    """Project one accepted steer or stop run control."""

    if kind == "start":
        raise ValueError("start controls are created by project_run_start")
    if kind == "steer" and input is None:
        raise ValueError("steer control requires input")
    return store.accept_run_control(
        run_id=run_id,
        kind=kind,
        timing=timing,
        locals=(
            (Local.typed("Part[]", tuple(input.parts), "_", 0),)
            if kind == "steer" and input is not None
            else (Local.typed("Text", input.content, "_", 0),)
            if kind == "stop" and input is not None
            else ()
        ),
        request_id=request_id,
        created_at=created_at or utc_now(),
    )


def project_step(
    store: RunStore,
    *,
    run_id: str | None = None,
    step_index: int | None = None,
    parent: StepPath | str | None = None,
    index: int | None = None,
    kind: StepKind,
    status: StepStatus,
    input: Sequence[Pointer],
    output: Sequence[Part] | Local | None,
    detail: Mapping[str, Any] | None = None,
    error: str | None = None,
    started_at: str,
    finished_at: str | None,
    context: Mapping[str, Any] | None = None,
) -> StepRecord:
    """Project step begin and, when terminal, step end events."""

    resolved_index = index if index is not None else step_index
    if run_id is None and parent is None or resolved_index is None:
        raise ValueError("project_step requires parent/index or run_id/step_index")
    path = (
        StepPath.parse(parent).child(resolved_index)
        if parent is not None
        else StepPath(str(run_id), (resolved_index,))
    )
    persist_event(
        store,
        StepBegin(
            step=path,
            kind=kind,
            input=tuple(input),
            placement=None,
            given=dict(context or {}),
            started_at=started_at,
        ),
    )
    if status != "running":
        persist_event(
            store,
            StepEnd(
                step=path,
                kind=kind,
                status=status,
                output=(
                    output
                    if isinstance(output, Local) or output is None
                    else Local.typed("Part[]", tuple(output), "_", 0)
                ),
                noted=dict(detail or {}),
                error=error,
                finished_at=finished_at or started_at,
            ),
        )
    step = next(
        (item for item in store.list_steps(run_id=path.run) if item.path == path),
        None,
    )
    if step is None:
        raise AssertionError(f"step was not projected: {path}")
    return step


def project_run_end(
    store: RunStore,
    *,
    run_id: str,
    status: RunStatus = "succeeded",
    error: str | None = None,
    finished_at: str | None = None,
    output: Local | Pointer | None = None,
) -> RunRecord:
    """Project one terminal run event, returning durable run truth."""

    persist_event(
        store,
        RunEnd(
            run=run_id,
            status=status,
            output=(
                Local.typed("Part[]", output, "_", 0)
                if isinstance(output, Pointer)
                else output
            ),
            error=error,
            finished_at=finished_at or utc_now(),
        ),
    )
    run = store.get_run(run_id=run_id)
    if run is None:
        raise AssertionError(f"run was not projected: {run_id}")
    return run
