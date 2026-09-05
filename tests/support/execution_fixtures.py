"""Build durable execution fixtures without running the executor."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Literal, cast

from toolang.base.types.message import Message, Part
from toolang.base.types.policy import RunBindings, RunLimits
from toolang.base.types.run import ModelCall, ToolCall
from toolang.execution.events import (
    RunBegin,
    RunEnd,
    RunEvent,
    StepBegin,
    StepEnd,
)
from toolang.execution.records import (
    ControlRecord,
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
    ErrorMessage,
    ErrorRef,
    FieldRef,
    IterationOccurrence,
    RunStatus,
    ModelStepGiven,
    ModelStepNoted,
    ModelTokenCount,
    ModelTokenPrice,
    Occurrence,
    OccurrencePosition,
    StepGiven,
    StepKind,
    StepNoted,
    StepStatus,
    StepRef,
    Local,
    ThreadRef,
    ToolStepGiven,
)
from toolang.lang.ast import (
    AskStmt,
    LetStmt,
    MapStmt,
    RepeatStmt,
    RunStmt,
    SeekStmt,
    Span,
)
from toolang.lang.input import RunnableInput
from toolang.lang.types import Array


_TEST_STATE = "0" * 64


def persist_event(store: RunStore, event: RunEvent) -> None:
    """Persist one trace event through the executor's internal projector."""

    sink = getattr(store, "_test_event_projector", None)
    if sink is None:
        sink = _PersistSink(store)
        setattr(store, "_test_event_projector", sink)
    cast(_PersistSink, sink).on_event(event)


def accept_run(
    store: RunStore,
    *,
    run_id: str,
    parent: StepRef | None,
    thread: str,
    input: Message | RunnableInput,
    context: Mapping[str, Any],
    request_id: str | None,
    created_at: str,
    kind: Literal["run", "rerun"] = "run",
    source: str | None = None,
    bindings: RunBindings | None = None,
    limits: RunLimits | None = None,
    resources: AgentResources | None = None,
    sandbox: str | None = None,
) -> tuple[RunRecord, ControlRecord]:
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

    state_ref = None
    if parent is not None:
        parent_record = next(
            (
                step
                for step in store.list_steps(run_id=parent.run_id)
                if step.ref == parent
            ),
            None,
        )
        if parent_record is None:
            raise ValueError(f"parent step not found: {parent}")
        state_ref = parent_record.state
    return store.accept_run(
        run_id=run_id,
        parent=parent,
        thread=thread,
        resources=resources if resources is not None else AgentResources(),
        limits=limits if limits is not None else RunLimits(),
        runnable=resolved_bindings.runnable or "agic:test",
        model=resolved_bindings.model or "test",
        locals=tuple(locals_value),
        sandbox=sandbox
        if sandbox is not None
        else ("host" if parent is None else None),
        occurrence=_occurrence_from_context(context),
        state=_TEST_STATE if parent is None else None,
        request_id=request_id,
        created_at=created_at,
        kind=kind,
        source=source,
        state_ref=state_ref,
    )


def project_run_start(
    store: RunStore,
    *,
    run_id: str,
    thread_id: str | ThreadRef,
    origin: str,
    input: Message,
    root_run_id: str | None = None,
    runnable_kind: str = "agic",
    runnable_name: str | None = None,
    call_kind: str = "top",
    metadata: Mapping[str, Any] | None = None,
    request_id: str | None = None,
    created_at: str | None = None,
    started_at: str | None = None,
    parent: StepRef | str | None = None,
    context: Mapping[str, Any] | None = None,
) -> RunRecord:
    """Project accepted and begun run events, returning durable run truth."""

    created = created_at or utc_now()
    started = started_at or created
    run_context = dict(context or metadata or {})
    del root_run_id
    del call_kind
    thread_id = str(thread_id)
    if store.get_thread(thread_id=thread_id) is None:
        store.create_thread(
            thread_id=thread_id,
            origin=origin,
            created_at=created,
        )
    parent_path = StepRef.parse(parent) if parent is not None else None
    state_ref = None
    if parent_path is not None:
        parent_record = next(
            (
                step
                for step in store.list_steps(run_id=parent_path.run_id)
                if step.ref == parent_path
            ),
            None,
        )
        if parent_record is None:
            raise ValueError(f"parent step not found: {parent_path}")
        state_ref = parent_record.state
    store.accept_run(
        run_id=run_id,
        parent=parent_path,
        thread=thread_id,
        resources=AgentResources(),
        limits=RunLimits(),
        runnable=(
            f"{runnable_kind}:{runnable_name}"
            if runnable_name is not None
            else f"{runnable_kind}:test"
        ),
        model="test",
        locals=(Local.typed("Part[]", tuple(input.parts), "_", 0),),
        sandbox="host" if parent_path is None else None,
        occurrence=_occurrence_from_context(run_context),
        state=_TEST_STATE if parent_path is None else None,
        request_id=request_id,
        created_at=created,
        state_ref=state_ref,
    )
    persist_event(
        store,
        RunBegin(
            run=run_id,
            parent=parent_path,
            control=ControlRef.for_run(run_id, 0),
            runnable=(
                f"{runnable_kind}:{runnable_name}"
                if runnable_name is not None
                else f"{runnable_kind}:test"
            ),
            occurrence=_occurrence_from_context(run_context),
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
) -> ControlRecord:
    """Project one accepted steer or cancel run control."""

    if kind == "run":
        raise ValueError("run controls are created by project_run_start")
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
            if kind == "cancel" and input is not None
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
    parent: StepRef | str | None = None,
    index: int | None = None,
    kind: StepKind,
    status: StepStatus,
    input: Sequence[FieldRef],
    output: Sequence[Part] | Local | None,
    detail: Mapping[str, Any] | None = None,
    error: str | ErrorMessage | ErrorRef | None = None,
    started_at: str,
    finished_at: str | None,
    context: Mapping[str, Any] | None = None,
) -> StepRecord:
    """Project step begin and, when terminal, step end events."""

    resolved_index = index if index is not None else step_index
    if run_id is None and parent is None or resolved_index is None:
        raise ValueError(
            "project_step requires parent with index or run_id with step_index"
        )
    path = (
        StepRef.parse(parent).child(resolved_index)
        if parent is not None
        else StepRef.from_local(str(run_id), (resolved_index,))
    )
    persist_event(
        store,
        StepBegin(
            step=path,
            kind=kind,
            input=tuple(input),
            occurrence=_occurrence_from_context(context or {}),
            given=_step_given(kind, context),
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
                noted=_step_noted(kind, detail),
                error=ErrorMessage(error) if isinstance(error, str) else error,
                finished_at=finished_at or started_at,
            ),
        )
    step = next(
        (item for item in store.list_steps(run_id=path.run_id) if item.ref == path),
        None,
    )
    if step is None:
        raise AssertionError(f"step was not projected: {path}")
    return step


def _step_given(
    kind: StepKind,
    context: Mapping[str, Any] | None,
) -> StepGiven:
    """Build minimal typed begin facts for persistence-focused tests."""

    facts = context or {}
    if kind == "model":
        model = facts.get("model")
        identity = (
            str(model.get("ref") or model.get("name") or "test")
            if isinstance(model, Mapping)
            else str(model or "test")
        )
        return ModelStepGiven(
            model=identity,
            call=ModelCall(instructions="", messages=[]),
        )
    if kind == "tool":
        plugin = str(facts.get("plugin") or facts.get("tool") or "test")
        return ToolStepGiven(
            plugin=plugin,
            call=ToolCall(
                tool_call_id=str(facts.get("tool_call_id") or "test-call"),
                call_id=str(facts.get("call_id") or "test-call"),
                name=str(facts.get("name") or "test"),
                input={},
            ),
        )

    span = Span(line=1)
    if kind == "value":
        return LetStmt(span=span, value="test")
    if kind == "run":
        return RunStmt(span=span, runnable="agic:test")
    if kind == "agent":
        return SeekStmt(span=span, name="test", runnable="agic:test")
    if kind == "human":
        return AskStmt(span=span, request="test")
    if kind == "par":
        return MapStmt(span=span, runnable="agic:test")
    if kind == "loop":
        return RepeatStmt(span=span, count=1)
    raise AssertionError(f"unsupported test Step kind: {kind}")


def _step_noted(
    kind: StepKind,
    detail: Mapping[str, Any] | None,
) -> StepNoted:
    """Convert legacy accounting details at the test boundary."""

    if kind != "model":
        return None
    facts = detail or {}
    raw_tokens = facts.get("tokens")
    tokens = (
        ModelTokenCount(
            input=int(raw_tokens.get("input", 0)),
            output=int(raw_tokens.get("output", 0)),
        )
        if isinstance(raw_tokens, Mapping)
        else None
    )
    raw_price = facts.get("price")
    price = (
        ModelTokenPrice(
            input=_optional_text(raw_price.get("input")),
            output=_optional_text(raw_price.get("output")),
        )
        if isinstance(raw_price, Mapping)
        else None
    )
    raw_cont = facts.get("cont")
    return ModelStepNoted(
        tokens=tokens,
        price=price,
        cost=_optional_text(facts.get("cost")),
        continuation=dict(raw_cont) if isinstance(raw_cont, Mapping) else None,
    )


def _optional_text(value: object) -> str | None:
    return str(value) if value is not None else None


def _occurrence_from_context(context: Mapping[str, Any]) -> Occurrence | None:
    raw = context.get("occurrence", context.get("placement"))
    if isinstance(raw, Occurrence):
        return raw
    if not isinstance(raw, Mapping):
        return None
    item = _occurrence_position(raw, "item", "items")
    lane = _occurrence_position(raw, "lane", "lanes")
    raw_iteration = raw.get("iteration", raw.get("iter"))
    iteration = None
    if isinstance(raw_iteration, IterationOccurrence):
        iteration = raw_iteration
    elif isinstance(raw_iteration, int) and not isinstance(raw_iteration, bool):
        raw_count = raw.get("iterations", raw.get("iters"))
        count = (
            raw_count
            if isinstance(raw_count, int)
            and not isinstance(raw_count, bool)
            and raw_count > raw_iteration >= 0
            else None
        )
        iteration = IterationOccurrence(
            index=max(raw_iteration, 0),
            count=count,
            phase="until" if raw_iteration < 0 else "body",
        )
    if item is None and lane is None and iteration is None:
        return None
    return Occurrence(item=item, lane=lane, iteration=iteration)


def _occurrence_position(
    raw: Mapping[str, Any],
    index_name: str,
    count_name: str,
) -> OccurrencePosition | None:
    index = raw.get(index_name)
    count = raw.get(count_name)
    if (
        isinstance(index, int)
        and not isinstance(index, bool)
        and isinstance(count, int)
        and not isinstance(count, bool)
        and 0 <= index < count
    ):
        return OccurrencePosition(index=index, count=count)
    return None


def project_run_end(
    store: RunStore,
    *,
    run_id: str,
    status: RunStatus = "succeeded",
    error: str | ErrorMessage | ErrorRef | None = None,
    finished_at: str | None = None,
    output: Local | FieldRef | None = None,
) -> RunRecord:
    """Project one terminal run event, returning durable run truth."""

    persist_event(
        store,
        RunEnd(
            run=run_id,
            status=status,
            output=(
                Local.typed("Part[]", output, "_", 0)
                if isinstance(output, FieldRef)
                else output
            ),
            error=ErrorMessage(error) if isinstance(error, str) else error,
            finished_at=finished_at or utc_now(),
        ),
    )
    run = store.get_run(run_id=run_id)
    if run is None:
        raise AssertionError(f"run was not projected: {run_id}")
    return run
