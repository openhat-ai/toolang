"""Compact references are durable facts, distinct from Step adoption."""

from dataclasses import replace
from pathlib import Path

import pytest
from pydantic import TypeAdapter

from tests.support.execution_fixtures import (
    project_run_end,
    project_run_start,
    project_step,
)
from toolang.base.types.message import Message
from toolang.base.types.run import ModelCall
from toolang.execution.records import (
    CompactControlPayload,
    ControlRecord,
    RunControlPayload,
    control_payload_from_data,
    control_payload_to_data,
)
from toolang.execution.store import RunStore
from toolang.execution.schemas import record_to_data
from toolang.execution.types import FieldRef, Local, ModelStepGiven, StepRef


def _compact_output(store: RunStore, *, indirect: bool = False) -> FieldRef:
    for run_id in ("run_earlier", "run_near"):
        project_run_start(
            store,
            run_id=run_id,
            thread_id="term_target",
            origin="test",
            input=Message.user(run_id),
        )
        project_run_end(store, run_id=run_id)
    source = project_run_start(
        store,
        run_id="run_summary",
        thread_id="compact_term_target",
        origin="test",
        input=Message.user("compact"),
    )
    output = Local(
        {
            "thread": "term_target",
            "begin": None,
            "end": "run_near",
            "summary": "Earlier facts.",
        }
    )
    if indirect:
        step = project_step(
            store,
            run_id=source.id,
            step_index=0,
            kind="value",
            status="succeeded",
            input=(),
            output=output,
            started_at="2026-09-06T00:00:00Z",
            finished_at="2026-09-06T00:00:01Z",
        )
        output = Local.typed("Json", FieldRef.from_path(step.ref, "output", "value"))
    project_run_end(store, run_id=source.id, output=output)
    return FieldRef.parse("run_summary/output")


@pytest.mark.parametrize("indirect", (False, True))
def test_initial_horizon_payload_roundtrip(tmp_path: Path, indirect: bool) -> None:
    store = RunStore(tmp_path / "runs.db")
    try:
        horizon = _compact_output(store, indirect=indirect)
        run = project_run_start(
            store,
            run_id="run_target",
            thread_id="term_target",
            origin="test",
            input=Message.user("continue"),
        )
        control = store.get_run_control(run_id=run.id, index=0)
        assert control is not None and isinstance(control.payload, RunControlPayload)
        assert control.payload.horizon is None
        payload = replace(control.payload, horizon=horizon)
        encoded = control_payload_to_data(payload)
        assert encoded["horizon"] == "run_summary/output"
        assert control_payload_from_data("run", encoded) == payload
        assert (
            control_payload_from_data("run", control_payload_to_data(control.payload))
            == control.payload
        )
        project_run_end(store, run_id=run.id)
        _, accepted = store.accept_run(
            run_id="run_next",
            parent=None,
            thread=str(run.thread),
            resources=payload.resources,
            limits=payload.limits,
            state=payload.state,
            runnable=payload.runnable,
            model=payload.model,
            input=payload.input,
            sandbox=payload.sandbox,
            horizon=horizon,
            occurrence=None,
            request_id=None,
            created_at="2026-09-06T00:00:01Z",
        )
        assert isinstance(accepted.payload, RunControlPayload)
        assert accepted.payload.horizon == horizon
        assert (
            TypeAdapter(ControlRecord).validate_python(record_to_data(accepted))
            == accepted
        )
    finally:
        store.close()
    store = RunStore(tmp_path / "runs.db", read_only=True)
    try:
        assert store.get_run_control(run_id="run_next", index=0) == accepted
    finally:
        store.close()


@pytest.mark.parametrize("triggered", (False, True))
def test_compact_reference_survives_restart_before_and_after_adoption(
    tmp_path: Path,
    triggered: bool,
) -> None:
    path = tmp_path / "runs.db"
    store = RunStore(path)
    horizon = _compact_output(store, indirect=triggered)
    run = project_run_start(
        store,
        run_id="run_target",
        thread_id="term_target",
        origin="test",
        input=Message.user("continue"),
    )
    trigger = (
        project_step(
            store,
            run_id=run.id,
            step_index=0,
            kind="tool",
            status="succeeded",
            input=(),
            output=(),
            started_at="2026-09-06T00:00:00Z",
            finished_at="2026-09-06T00:00:01Z",
        )
        if triggered
        else None
    )
    initial = store.get_run_control(run_id=run.id, index=0)
    revision = store.latest_run_control_revision()
    control = store.accept_compact_control(
        run_id=run.id,
        horizon=horizon,
        triggered_by=trigger.ref if trigger is not None else None,
        created_at="2026-09-06T00:00:01Z",
    )
    assert control.kind == "compact"
    assert control.payload == CompactControlPayload(horizon)
    assert control.status == "applied"
    assert control.triggered_by == (trigger.ref if trigger is not None else None)
    assert control_payload_to_data(control.payload) == {"horizon": str(horizon)}
    assert store.changed_run_controls(after_revision=revision)[1] == (control,)
    assert store.list_steps(run_id=run.id) == ([trigger] if trigger is not None else [])
    assert store.get_run_control(run_id=run.id, index=0) == initial
    store.close()

    store = RunStore(path)
    try:
        assert store.get_run_control(run_id=run.id, index=control.index) == control
        step = store.begin_step(
            ref=StepRef.from_local(run.id, (1,)),
            kind="model",
            input=(),
            preceded_by=(control.ref,),
            state=run.state,
            given=ModelStepGiven("test", ModelCall(instructions="", messages=[])),
            started_at="2026-09-06T00:00:02Z",
        )
    finally:
        store.close()
    store = RunStore(path, read_only=True)
    try:
        assert store.get_step(ref=step.ref) == step
        assert store.get_run_control(run_id=run.id, index=0) == initial
    finally:
        store.close()


def test_compact_rejects_inactive_run_without_writing_a_control(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs.db")
    try:
        run = project_run_start(
            store,
            run_id="run_target",
            thread_id="term_target",
            origin="test",
            input=Message.user("continue"),
        )
        project_run_end(store, run_id=run.id)
        before = store.list_run_controls(run_id=run.id)
        with pytest.raises(ValueError, match="run is not active"):
            store.accept_compact_control(
                run_id=run.id,
                horizon=FieldRef.parse("run_summary/output"),
                triggered_by=None,
                created_at="2026-09-06T00:00:01Z",
            )
        assert store.list_run_controls(run_id=run.id) == before
    finally:
        store.close()


@pytest.mark.parametrize("kind", ("run", "compact"))
@pytest.mark.parametrize(
    ("reference", "output"),
    (
        ("run_missing/output", Local({"thread": "term_target"})),
        ("run_summary/output", None),
        ("run_summary/control", Local({"thread": "term_target"})),
        ("term_target/id", Local({"thread": "term_target"})),
        ("run_summary/output", Local({"thread": "term_other"})),
        ("run_summary/output", Local("not a compact result")),
        (
            "run_summary/output",
            Local.typed("Json", FieldRef.parse("run_missing/output/value")),
        ),
    ),
)
def test_invalid_horizon_is_rejected_before_any_records_change(
    tmp_path: Path,
    kind: str,
    reference: str,
    output: Local | None,
) -> None:
    store = RunStore(tmp_path / "runs.db")
    try:
        run = project_run_start(
            store,
            run_id="run_target",
            thread_id="term_target",
            origin="test",
            input=Message.user("continue"),
        )
        summary = project_run_start(
            store,
            run_id="run_summary",
            thread_id="compact_term_target",
            origin="test",
            input=Message.user("compact"),
        )
        project_run_end(store, run_id=summary.id, output=output)
        entry = store.get_run_control(run_id=run.id, index=0)
        assert entry is not None and isinstance(entry.payload, RunControlPayload)
        payload = entry.payload
        before = tuple(store._conn.iterdump())
        with pytest.raises(ValueError):
            if kind == "compact":
                store.accept_compact_control(
                    run_id=run.id,
                    horizon=FieldRef.parse(reference),
                    triggered_by=None,
                    created_at="2026-09-06T00:00:01Z",
                )
            else:
                store.accept_run(
                    run_id="run_next",
                    parent=None,
                    thread=str(run.thread),
                    resources=payload.resources,
                    limits=payload.limits,
                    state=payload.state,
                    runnable=payload.runnable,
                    model=payload.model,
                    input=payload.input,
                    sandbox=payload.sandbox,
                    horizon=FieldRef.parse(reference),
                    occurrence=None,
                    request_id=None,
                    created_at="2026-09-06T00:00:01Z",
                )
        assert tuple(store._conn.iterdump()) == before
    finally:
        store.close()
