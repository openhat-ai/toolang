from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from toolang.base.types.message import Message, TextPart
from toolang.base.types.policy import RunLimits
from toolang.base.types.run import ModelCall
from toolang.execution.records import (
    CancelControlPayload,
    ControlRecord,
    CreateControlPayload,
    ExecuteControlPayload,
    ForkControlPayload,
    ReloadControlPayload,
    RerunControlPayload,
    RetryControlPayload,
    RewindControlPayload,
    RunControlPayload,
    SteerControlPayload,
    execution_error_from_data,
    execution_error_to_data,
    model_call_from_data,
    model_call_to_data,
)
from toolang.execution.schemas import record_to_data
from toolang.execution.store import RunStore
from toolang.execution.types import (
    AgentResources,
    ControlRef,
    ErrorMessage,
    ErrorRef,
    FieldRef,
    Local,
    Pointer,
    RunRef,
    StepRef,
    ThreadRef,
)
from tests.support.execution_fixtures import (
    project_run_end,
    project_run_start,
    project_step,
)


def test_model_call_payload_uses_output_schema_and_compact_cont_key() -> None:
    call = ModelCall(
        instructions="Return the value.",
        messages=[Message.user("Decide")],
        output_schema={"type": "boolean"},
        continuation={"cursor": "next"},
    )

    data = model_call_to_data(call)

    assert data == {
        "instructions": "Return the value.",
        "messages": [
            {
                "role": "user",
                "parts": [{"type": "text", "text": "Decide"}],
            }
        ],
        "tools": [],
        "output_schema": {"type": "boolean"},
        "cont": {"cursor": "next"},
    }
    assert model_call_from_data(data) == call

    legacy_data = {**data, "structured_output": data["output_schema"]}
    legacy_data.pop("output_schema")
    assert model_call_from_data(legacy_data) == call


def test_execution_error_codec_uses_strict_tagged_objects() -> None:
    message = ErrorMessage("model request timed out")
    reference = ErrorRef(FieldRef.from_path(StepRef.parse("run_root.0"), "error"))

    assert execution_error_to_data(message) == {
        "type": "message",
        "message": "model request timed out",
    }
    assert execution_error_to_data(reference) == {
        "type": "ref",
        "ref": "run_root.0/error",
    }
    assert execution_error_from_data(execution_error_to_data(message)) == message
    assert execution_error_from_data(execution_error_to_data(reference)) == reference


@pytest.mark.parametrize(
    "value",
    (
        None,
        "failure",
        {},
        {"type": "unknown", "message": "failure"},
        {"type": "message"},
        {"type": "message", "message": "failure", "extra": True},
        {"type": "ref", "ref": "run_root.0/output"},
        {"type": "ref", "ref": "term_root/error"},
    ),
)
def test_execution_error_codec_rejects_noncanonical_values(value: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        execution_error_from_data(value)


def test_error_resolution_rejects_missing_null_and_cyclic_targets(
    tmp_path: Path,
) -> None:
    store = RunStore(tmp_path / "runs.db")
    try:
        run = project_run_start(
            store,
            run_id="run_errors",
            thread_id="term_errors",
            origin="test",
            input=Message.user("hello"),
        )
        first = StepRef.from_local(run.id, (0,))
        second = StepRef.from_local(run.id, (1,))
        empty = StepRef.from_local(run.id, (2,))
        project_step(
            store,
            run_id=run.id,
            step_index=0,
            kind="value",
            status="failed",
            input=(),
            output=None,
            error=ErrorRef(FieldRef.from_path(second, "error")),
            started_at="2026-01-01T00:00:00Z",
            finished_at="2026-01-01T00:00:01Z",
        )
        project_step(
            store,
            run_id=run.id,
            step_index=1,
            kind="value",
            status="failed",
            input=(),
            output=None,
            error=ErrorRef(FieldRef.from_path(first, "error")),
            started_at="2026-01-01T00:00:01Z",
            finished_at="2026-01-01T00:00:02Z",
        )
        project_step(
            store,
            run_id=run.id,
            step_index=2,
            kind="value",
            status="succeeded",
            input=(),
            output=None,
            started_at="2026-01-01T00:00:02Z",
            finished_at="2026-01-01T00:00:03Z",
        )

        with pytest.raises(ValueError, match="error reference cycle"):
            store.resolve_error(ErrorRef(FieldRef.from_path(first, "error")))
        with pytest.raises(ValueError, match="record not found"):
            store.resolve_error(
                ErrorRef(
                    FieldRef.from_path(
                        StepRef.from_local(run.id, (9,)),
                        "error",
                    )
                )
            )
        with pytest.raises(ValueError, match="target has no error"):
            store.resolve_error(ErrorRef(FieldRef.from_path(empty, "error")))
    finally:
        store.close()


def test_record_registry_serializes_exact_record_shapes(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs.db")
    try:
        run = project_run_start(
            store,
            run_id="run_records",
            thread_id="term_records",
            origin="test",
            input=Message.user("hello"),
        )
        step = project_step(
            store,
            run_id=run.id,
            step_index=0,
            kind="value",
            status="succeeded",
            input=(
                FieldRef.from_path(
                    ControlRef.for_run(run.id, 0), "payload", "locals", 0, "value"
                ),
            ),
            output=(TextPart("result"),),
            started_at="2026-01-01T00:00:01Z",
            finished_at="2026-01-01T00:00:02Z",
        )
        project_run_end(
            store,
            run_id=run.id,
            output=Local.typed(
                "Part[]",
                FieldRef.from_path(step.ref, "output", "value"),
                "_",
            ),
        )
        thread = store.get_thread(thread_id=str(run.thread))
        control = store.get_run_control(run_id=run.id, index=0)
        stored_run = store.get_run(run_id=run.id)
        stored_step = store.get_step(ref=step.ref)
        assert thread is not None
        assert control is not None
        assert stored_run is not None
        assert stored_step is not None

        thread_data = record_to_data(thread)
        control_data = record_to_data(control)
        run_data = record_to_data(stored_run)
        step_data = record_to_data(stored_step)

        assert set(thread_data) == {
            "id",
            "origin",
            "peer",
            "created_at",
            "updated_at",
        }
        assert set(control_data) == {
            "id",
            "kind",
            "payload",
            "request",
            "status",
            "timing",
            "error",
            "created_at",
            "finished_at",
        }
        assert set(run_data) == {
            "id",
            "parent",
            "thread",
            "control",
            "state",
            "output",
            "occur",
            "status",
            "error",
            "created_at",
            "started_at",
            "finished_at",
        }
        assert set(step_data) == {
            "id",
            "kind",
            "input",
            "given",
            "state",
            "output",
            "occur",
            "noted",
            "status",
            "error",
            "created_at",
            "started_at",
            "finished_at",
        }
        assert "scope" not in control_data
        assert control_data["id"] == f"{run.id}@0"
        assert step_data["input"] == [f"{run.id}@0/payload/locals/0/value"]
        assert run_data["output"] == {
            "type": "Part[]",
            "value": {"?": f"{step.ref}/output/value:Part[]"},
            "name": "_",
            "dim": 0,
        }
        assert record_to_data(
            replace(stored_run, error=ErrorRef(FieldRef.from_path(step.ref, "error")))
        )["error"] == {"type": "ref", "ref": f"{step.ref}/error"}
    finally:
        store.close()


def test_model_step_record_serializes_compact_noted_cont_key(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs.db")
    try:
        run = project_run_start(
            store,
            run_id="run_model_noted",
            thread_id="term_model_noted",
            origin="test",
            input=Message.user("hello"),
        )
        step = project_step(
            store,
            run_id=run.id,
            step_index=0,
            kind="model",
            status="succeeded",
            input=(),
            output=(TextPart("true"),),
            detail={"cont": {"cursor": "next"}},
            started_at="2026-01-01T00:00:01Z",
            finished_at="2026-01-01T00:00:02Z",
        )

        noted = record_to_data(step)["noted"]

        assert noted == {
            "tokens": None,
            "price": None,
            "cost": None,
            "accounting": None,
            "cont": {"cursor": "next"},
        }
    finally:
        store.close()


def test_record_selection_matches_rfc6901_traversal(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs.db")
    try:
        run = project_run_start(
            store,
            run_id="run_select",
            thread_id="term_select",
            origin="test",
            input=Message.user("hello"),
        )
        step = project_step(
            store,
            run_id=run.id,
            step_index=0,
            kind="value",
            status="succeeded",
            input=(),
            output=(TextPart("first"), TextPart("second")),
            started_at="2026-01-01T00:00:01Z",
            finished_at="2026-01-01T00:00:02Z",
        )

        whole = store.select_pointer(Pointer.parse(str(step.ref)))
        output = store.select_pointer(
            Pointer(FieldRef.from_path(step.ref, "output", "value", 1))
        )
        missing_null = store.select_pointer(
            Pointer(FieldRef.from_path(step.ref, "error"))
        )

        assert output.value == whole.value["output"]["value"][1]  # type: ignore[index]
        assert output.runtime == TextPart("second")
        assert output.type_name == "Part"
        assert missing_null.value is None
        assert missing_null.type_name == "ErrorMessage | ErrorRef | None"
        assert missing_null.render_type == "ErrorMessage | ErrorRef | None"
        with pytest.raises(ValueError, match="field does not exist"):
            store.select_pointer(Pointer(FieldRef.from_path(step.ref, "missing")))
        with pytest.raises(ValueError, match="invalid array index"):
            store.select_pointer(
                Pointer(FieldRef.from_path(step.ref, "output", "value", "01"))
            )
        with pytest.raises(ValueError, match="traverses a scalar"):
            store.select_pointer(
                Pointer(FieldRef.from_path(step.ref, "status", "child"))
            )
    finally:
        store.close()


def test_control_pointer_uses_one_record_lookup(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs.db")
    try:
        run = project_run_start(
            store,
            run_id="run_lookup",
            thread_id="term_lookup",
            origin="test",
            input=Message.user("hello"),
        )
        queries: list[str] = []
        store._conn.set_trace_callback(queries.append)

        record = store.get_record(Pointer(ControlRef.for_run(run.id, 0)))

        assert isinstance(record, ControlRecord)
        selects = [
            query for query in queries if query.lstrip().upper().startswith("SELECT")
        ]
        assert len(selects) == 1
        assert "FROM controls" in selects[0]
    finally:
        store.close()


def test_record_lookup_retains_steps_owned_by_a_rewound_run(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs.db")
    try:
        run = project_run_start(
            store,
            run_id="run_ejected",
            thread_id="term_ejected",
            origin="test",
            input=Message.user("hello"),
        )
        step = project_step(
            store,
            run_id=run.id,
            step_index=0,
            kind="value",
            status="succeeded",
            input=(),
            output=(TextPart("result"),),
            started_at="2026-01-01T00:00:01Z",
            finished_at="2026-01-01T00:00:02Z",
        )
        project_run_end(store, run_id=run.id)
        thread = store.get_thread(thread_id=str(run.thread))
        assert thread is not None
        store.rewind_thread(
            thread_id=thread.id,
            anchor=run.id,
            request_id=None,
            expected_head=store.thread_views().head(thread.id),
            created_at="2026-01-01T00:00:03Z",
        )

        assert store.get_record(Pointer(step.ref)) == step
        assert store.inspect_runs(thread_id=thread.id) == ()
    finally:
        store.close()


@pytest.mark.parametrize("reopen", (False, True))
def test_record_lookup_retains_children_of_a_rewound_run(
    tmp_path: Path, reopen: bool
) -> None:
    store = RunStore(tmp_path / "runs.db")
    try:
        root = project_run_start(
            store,
            run_id="run_parent",
            thread_id="term_parent",
            origin="test",
            input=Message.user("hello"),
        )
        parent = project_step(
            store,
            run_id=root.id,
            step_index=0,
            kind="run",
            status="succeeded",
            input=(),
            output=(TextPart("parent"),),
            started_at="2026-01-01T00:00:01Z",
            finished_at="2026-01-01T00:00:02Z",
        )
        child = project_run_start(
            store,
            run_id="run_child",
            thread_id=root.thread,
            origin="test",
            input=Message.user("child"),
            parent=parent.ref,
        )
        child_step = project_step(
            store,
            run_id=child.id,
            step_index=0,
            kind="value",
            status="succeeded",
            input=(),
            output=(TextPart("child"),),
            started_at="2026-01-01T00:00:03Z",
            finished_at="2026-01-01T00:00:04Z",
        )
        project_run_end(store, run_id=child.id)
        project_run_end(store, run_id=root.id)
        store.rewind_thread(
            thread_id=str(root.thread),
            anchor=root.id,
            request_id=None,
            expected_head=store.thread_views().head(str(root.thread)),
            created_at="2026-01-01T00:00:05Z",
        )
        if reopen:
            store.close()
            store = RunStore(tmp_path / "runs.db")

        assert child.id in {run.id for run in store.list_runs(limit=None)}
        assert store.get_record(Pointer.parse(child.id)) == store.get_run(
            run_id=child.id
        )
        assert store.get_record(Pointer(ControlRef.for_run(child.id, 0))) is not None
        assert store.get_record(Pointer(child_step.ref)) == child_step
        assert store.inspect_runs(thread_id=str(root.thread)) == ()
        assert store.list_runs(thread_id=str(root.thread)) == []
        assert store.list_thread_history_chronological(thread_id=str(root.thread)) == ()
        assert {
            run.id
            for run in store.list_runs(thread_id=str(root.thread), include_rewound=True)
        } == {root.id, child.id}
        assert store.inspect_child_runs(parent=parent)[0].record.id == child.id
        assert store.inspect_steps(run_id=child.id)[0].record == child_step
        assert {item.record.id for item in store.inspect_runs()} == {root.id, child.id}
        snapshot = store.load_execution_snapshot(root=root.id)
        assert {run.id for run in snapshot.runs} == {root.id, child.id}
        assert {step.ref for step in snapshot.steps} == {parent.ref, child_step.ref}
        child_snapshot = store.load_execution_snapshot(root=parent.ref)
        assert {run.id for run in child_snapshot.runs} == {child.id}
        assert {step.ref for step in child_snapshot.steps} == {
            parent.ref,
            child_step.ref,
        }
    finally:
        store.close()


def test_every_control_payload_variant_has_one_canonical_record_shape() -> None:
    revision = "a" * 64
    resources = AgentResources()
    limits = RunLimits()
    source = FieldRef.parse("run_control.0/output/value/0")
    cases = (
        (
            "run",
            RunControlPayload(
                resources, limits, revision, "agic:test", "test/model", ()
            ),
            {
                "resources",
                "limits",
                "state",
                "runnable",
                "model",
                "model_request",
                "locals",
                "sandbox",
                "authored_input",
                "authored_commands",
                "authored_session_commands",
                "prompt_invocations",
            },
        ),
        (
            "rerun",
            RerunControlPayload(
                resources,
                limits,
                revision,
                "agic:test",
                "test/model",
                (),
                RunRef("run_source"),
            ),
            {
                "resources",
                "limits",
                "state",
                "runnable",
                "model",
                "model_request",
                "locals",
                "rerun_from",
                "sandbox",
                "authored_input",
                "authored_commands",
                "authored_session_commands",
                "prompt_invocations",
            },
        ),
        (
            "retry",
            RetryControlPayload(
                resources,
                limits,
                revision,
                "agic:test",
                "test/model",
                None,
                StepRef.from_local("run_control", (0,)),
            ),
            {
                "resources",
                "limits",
                "state",
                "runnable",
                "model",
                "model_request",
                "locals",
                "retry_from",
                "sandbox",
                "authored_input",
                "authored_commands",
                "authored_session_commands",
                "prompt_invocations",
            },
        ),
        ("reload", ReloadControlPayload(revision), {"state"}),
        (
            "execute",
            ExecuteControlPayload(
                revision,
                "agic:next",
                "agent",
                source,
                (Local.typed("Json", source.select("input", "input", "_"), "_"),),
            ),
            {"state", "runnable", "module", "source", "locals"},
        ),
        (
            "steer",
            SteerControlPayload((Local.typed("Text", "continue", "_"),)),
            {"locals"},
        ),
        ("cancel", CancelControlPayload(), {"locals"}),
        ("create", CreateControlPayload(), set()),
        (
            "fork",
            ForkControlPayload(
                ThreadRef("term_source"),
                RunRef("run_source"),
                ControlRef.for_thread("term_source", 0),
            ),
            {"fork_from", "fork_at", "fork_head"},
        ),
        (
            "rewind",
            RewindControlPayload(
                RunRef("run_source"),
                RunRef("run_end"),
                ControlRef.for_thread("term_control", 2),
            ),
            {"rewind_from", "rewind_through", "rewind_if"},
        ),
    )

    for index, (kind, payload, payload_fields) in enumerate(cases):
        target = (
            "term_control" if kind in {"create", "fork", "rewind"} else "run_control"
        )
        data = record_to_data(
            ControlRecord(id=f"{target}@{index}", kind=kind, payload=payload)  # type: ignore[arg-type]
        )

        assert set(data) == {
            "id",
            "kind",
            "payload",
            "request",
            "status",
            "timing",
            "error",
            "created_at",
            "finished_at",
        }
        assert set(data["payload"]) == payload_fields  # type: ignore[arg-type]
