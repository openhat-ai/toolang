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
    model_call_from_data,
    model_call_to_data,
)
from toolang.execution.schemas import record_to_data
from toolang.execution.store import RunStore
from toolang.execution.types import AgentResources, Local, Pointer, StepPath
from tests.support.execution_fixtures import (
    project_run_end,
    project_run_start,
    project_step,
)


def test_model_call_payload_uses_structured_output_and_compact_cont_key() -> None:
    call = ModelCall(
        instructions="Return the value.",
        messages=[Message.user("Decide")],
        structured_output={"type": "boolean"},
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
        "structured_output": {"type": "boolean"},
        "cont": {"cursor": "next"},
    }
    assert model_call_from_data(data) == call


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
            input=(Pointer.control(run.id, 0, "payload", "locals", 0, "value"),),
            output=(TextPart("result"),),
            started_at="2026-01-01T00:00:01Z",
            finished_at="2026-01-01T00:00:02Z",
        )
        project_run_end(
            store,
            run_id=run.id,
            output=Local.typed(
                "Part[]",
                Pointer.step(step.path, "output", "value"),
                "_",
            ),
        )
        thread = store.get_thread(thread_id=run.thread)
        control = store.get_run_control(run_id=run.id, index=0)
        stored_run = store.get_run(run_id=run.id)
        stored_step = store.get_step(path=step.path)
        assert thread is not None
        assert control is not None
        assert stored_run is not None
        assert stored_step is not None

        thread_data = record_to_data(thread)
        control_data = record_to_data(control)
        run_data = record_to_data(stored_run)
        step_data = record_to_data(stored_step)

        assert set(thread_data) == {
            "thread_id",
            "origin",
            "peer",
            "created_by",
            "head",
            "created_at",
            "updated_at",
        }
        assert set(control_data) == {
            "target",
            "index",
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
            "occurrence",
            "status",
            "error",
            "ejected_by",
            "created_at",
            "started_at",
            "finished_at",
        }
        assert set(step_data) == {
            "path",
            "kind",
            "input",
            "given",
            "state",
            "output",
            "occurrence",
            "noted",
            "status",
            "error",
            "ejected_by",
            "created_at",
            "started_at",
            "finished_at",
        }
        assert "scope" not in control_data
        assert control_data["target"] == run.id
        assert step_data["input"] == [f"{run.id}@0/payload/locals/0/value"]
        assert run_data["output"] == {
            "type": "Part[]",
            "value": {"?": f"{step.path}/output/value:Part[]"},
            "name": "_",
            "dim": 0,
        }
        assert (
            record_to_data(replace(stored_run, error=Pointer.step(step.path, "error")))[
                "error"
            ]
            == f"{step.path}/error"
        )
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

        whole = store.select_pointer(Pointer(str(step.path)))
        output = store.select_pointer(Pointer.step(step.path, "output", "value", 1))
        missing_null = store.select_pointer(Pointer.step(step.path, "error"))

        assert output.value == whole.value["output"]["value"][1]  # type: ignore[index]
        assert output.runtime == TextPart("second")
        assert output.type_name == "Part"
        assert missing_null.value is None
        assert missing_null.type_name == "ExecutionError | None"
        assert missing_null.render_type == "ExecutionError | None"
        with pytest.raises(ValueError, match="field does not exist"):
            store.select_pointer(Pointer.step(step.path, "missing"))
        with pytest.raises(ValueError, match="invalid array index"):
            store.select_pointer(Pointer.step(step.path, "output", "value", "01"))
        with pytest.raises(ValueError, match="traverses a scalar"):
            store.select_pointer(Pointer.step(step.path, "status", "child"))
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

        record = store.get_record(Pointer.control(run.id, 0))

        assert isinstance(record, ControlRecord)
        selects = [
            query for query in queries if query.lstrip().upper().startswith("SELECT")
        ]
        assert len(selects) == 1
        assert "FROM controls" in selects[0]
    finally:
        store.close()


def test_record_lookup_hides_steps_owned_by_an_ejected_run(tmp_path: Path) -> None:
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
        thread = store.get_thread(thread_id=run.thread)
        assert thread is not None
        store.rewind_thread(
            thread_id=thread.thread_id,
            anchor=run.id,
            request_id=None,
            expected_head=thread.head,
            created_at="2026-01-01T00:00:03Z",
        )

        assert store.get_record(Pointer(str(step.path))) is None
    finally:
        store.close()


def test_record_lookup_hides_a_run_owned_by_an_ejected_step(tmp_path: Path) -> None:
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
            parent=parent.path,
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
        with store.write_transaction():
            store._conn.execute(
                """
                UPDATE steps
                SET ejected_by_target = ?, ejected_by_index = ?
                WHERE run = ? AND path = ?
                """,
                (root.thread, 0, parent.path.run, parent.path.local),
            )

        assert child.id not in {run.id for run in store.list_runs(limit=None)}
        assert store.get_record(Pointer(child.id)) is None
        assert store.get_record(Pointer.control(child.id, 0)) is None
        assert store.get_record(Pointer(str(child_step.path))) is None
    finally:
        store.close()


def test_every_control_payload_variant_has_one_canonical_record_shape() -> None:
    revision = "a" * 64
    resources = AgentResources()
    limits = RunLimits()
    source = Pointer("run_control.0/output/value/0")
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
                "run_source",
            ),
            {
                "resources",
                "limits",
                "state",
                "runnable",
                "model",
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
                StepPath("run_control", (0,)),
            ),
            {
                "resources",
                "limits",
                "state",
                "runnable",
                "model",
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
            ForkControlPayload("term_source", "run_source"),
            {"fork_from", "fork_at"},
        ),
        (
            "rewind",
            RewindControlPayload("run_source", 2),
            {"rewind_from", "rewind_if"},
        ),
    )

    for index, (kind, payload, payload_fields) in enumerate(cases):
        target = (
            "term_control" if kind in {"create", "fork", "rewind"} else "run_control"
        )
        data = record_to_data(
            ControlRecord(target=target, index=index, kind=kind, payload=payload)  # type: ignore[arg-type]
        )

        assert set(data) == {
            "target",
            "index",
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


def test_durable_record_json_rejects_colon_bearing_field_names(
    tmp_path: Path,
) -> None:
    store = RunStore(tmp_path / "runs.db")
    try:
        store.create_thread(thread_id="term_colon", origin="test")

        with pytest.raises(ValueError, match="cannot contain ':'"):
            store.accept_run(
                run_id="run_colon",
                parent=None,
                thread="term_colon",
                resources=AgentResources(),
                limits=RunLimits(),
                state="a" * 64,
                runnable="agic:test",
                model="test/model",
                locals=(Local.typed("Json", {"bad:name": 1}, "_"),),
                sandbox="host",
                occurrence=None,
                request_id=None,
                created_at="2026-01-01T00:00:00Z",
            )

        assert store.get_run(run_id="run_colon") is None
        assert store.list_run_controls(run_id="run_colon") == ()
    finally:
        store.close()
