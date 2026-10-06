"""Output bindings are independent of reusable complete values."""

from contextlib import closing
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest
from pydantic import TypeAdapter

from tests.support.execution_fixtures import (
    project_run_end,
    project_run_start,
    project_step,
)
from toolang.base.types.message import Message, TextPart
from toolang.execution.events import RunEnd, run_event_from_data, run_event_to_data
from toolang.execution.records import output_from_data, output_to_data
from toolang.execution.store import RunStore
from toolang.execution.types import (
    FieldRef,
    Output,
    Pointer,
    TypedRef,
    output_from_protocol_data,
    output_to_protocol_data,
    value_to_protocol_data,
)
from toolang.lang.types import Array, Struct, Value


@pytest.mark.parametrize("binding", [None, "_", "answer"])
@pytest.mark.parametrize(
    "value",
    [
        "",
        0,
        False,
        None,
        Array("Part[]", (TextPart("answer"),)),
        Array("Text[]", ()),
        Array("Text[][]", (Array("Text[]", ("nested",)),)),
        TypedRef(FieldRef.parse("run_source.0/output/value"), "Text[]"),
    ],
)
def test_output_reuses_value_across_storage_and_protocol(
    value: Value | TypedRef, binding: str | None
) -> None:
    output = Output(value, binding)
    assert output.value is value
    stored = output_to_data(output)
    assert set(stored) == {"type", "value", "binding"}
    assert output_from_data(stored) == output
    assert output_from_data({k: v for k, v in stored.items() if k != "type"}) == output
    data = output_to_protocol_data(output)
    assert data == {
        "type": output.type,
        "value": value_to_protocol_data(value),
        "binding": binding,
    }
    assert output_from_protocol_data(data) == output
    adapter = TypeAdapter(Output)
    assert adapter.dump_python(output, mode="json") == data
    assert adapter.validate_json(adapter.dump_json(output)) == output
    event = RunEnd(run="run_result", status="succeeded", output=output)
    assert run_event_from_data(run_event_to_data(event)) == event


def test_output_binding_is_validated_and_immutable() -> None:
    output = Output("result", "_")
    with pytest.raises(FrozenInstanceError):
        setattr(output, "binding", "answer")
    for binding in ("", "bad name", "bad-name", 1, False):
        with pytest.raises(ValueError, match="invalid output binding"):
            Output(output.value, binding)  # ty: ignore[invalid-argument-type]


@pytest.mark.parametrize("binding", [None, "_", "answer"])
@pytest.mark.parametrize(
    "output", [None, Output(None), Output(Array("Text[]", ("a", "b")))]
)
def test_output_presence_bindings_and_references_survive_reopening(
    tmp_path: Path, output: Output | None, binding: str | None
) -> None:
    path = tmp_path / "runs.db"
    output = replace(output, binding=binding) if output is not None else None
    with closing(RunStore(path)) as store:
        run = project_run_start(
            store,
            run_id="run_result",
            thread_id="term_result",
            origin="test",
            input=Message.user("input"),
        )
        step = project_step(
            store,
            run_id=run.id,
            step_index=0,
            kind="value",
            status="succeeded",
            input=(),
            output=output,
            started_at="2026-01-01T00:00:00Z",
            finished_at="2026-01-01T00:00:01Z",
        )
        reference = FieldRef.from_path(step.ref, "output", "value")
        retained = (
            Output(TypedRef(reference, output.type), binding)
            if output is not None
            else None
        )
        project_run_end(store, run_id=run.id, output=retained)

    with closing(RunStore(path)) as store:
        restored = store.get_step(ref=step.ref)
        final = store.get_run(run_id=run.id)
        assert restored is not None and final is not None
        assert restored.output == output
        assert final.output == retained
        if output is None:
            assert final.output is None
        else:
            assert final.output is not None
            assert store.resolve_output(final.output) == output
            selected = store.select_pointer(Pointer(reference))
            assert selected.runtime == output.value
            assert selected.render_type == output.type
            assert (
                store.select_pointer(
                    Pointer(FieldRef.from_path(step.ref, "output"))
                ).runtime
                == output
            )
            assert (
                store.select_pointer(
                    Pointer(FieldRef.from_path(step.ref, "output", "binding"))
                ).runtime
                == binding
            )
            for suffix in (("local",), ("local", "value"), ("dim",)):
                with pytest.raises(ValueError, match="field does not exist"):
                    store.select_pointer(
                        Pointer(FieldRef.from_path(step.ref, "output", *suffix))
                    )


@pytest.mark.parametrize(
    "data",
    [
        {"value": "old", "name": "_", "dim": 0},
        {"local": {"value": "old", "dim": 0}, "binding": "_"},
        {"local": {"type": "Text", "value": "old"}, "binding": "_"},
        {"local": None, "binding": "_"},
    ],
)
def test_output_codecs_reject_removed_wrappers(data) -> None:
    for decode in (output_from_data, output_from_protocol_data):
        with pytest.raises(ValueError):
            decode(data)


@pytest.mark.parametrize("binding", [None, "job"])
@pytest.mark.parametrize(
    "result_type", [None, "Text", "Report[]", "Part[]", "Text[][]"]
)
def test_run_handle_has_one_protocol_and_event_shape(binding, result_type):
    from toolang.execution.types import AwaitableHandle
    from toolang.execution.events import StepEnd
    from toolang.execution.types import StepRef

    handle = AwaitableHandle("run_spawned", "spawn_thread", result_type)
    output = Output(handle, binding)
    stored = output_to_data(output)
    assert stored == {
        "type": "_Awaitable",
        "value": {
            "kind": "run",
            "id": "run_spawned",
            "thread": "spawn_thread",
            "result_type": result_type,
        },
        "binding": binding,
    }
    assert stored == output_to_protocol_data(output)
    assert output_from_data(stored) == output
    assert output_from_protocol_data(stored) == output
    adapter = TypeAdapter(Output)
    assert adapter.validate_json(adapter.dump_json(output)) == output
    assert adapter.dump_python(output, mode="json") == stored
    event = StepEnd(
        step=StepRef.parse("run_source.0"),
        kind="spawn",
        status="succeeded",
        output=output,
    )
    assert run_event_from_data(run_event_to_data(event)) == event
    assert set(adapter.json_schema()["properties"]) == {"type", "value", "binding"}


def test_authored_run_struct_and_json_are_not_runtime_handles():
    fields = {"id": "run_spawned", "thread": "spawn_thread"}
    for value, expected_type in ((Struct("Run", fields), "Run"), (fields, "Json")):
        output = Output(value, "job")
        assert output.type == expected_type
        assert output_from_protocol_data(output_to_protocol_data(output)) == output
        assert output_from_data(output_to_data(output)) == output


@pytest.mark.parametrize(
    "value",
    [
        {"id": "run_spawned"},
        {"id": "invalid/id", "thread": "spawn_thread"},
        {"id": "run_spawned", "thread": "spawn_thread", "status": "pending"},
        {"id": "run_spawned", "thread": "spawn_thread", "result": None},
    ],
)
def test_runtime_handle_output_requires_only_canonical_identity(value):
    for decode in (output_from_data, output_from_protocol_data):
        with pytest.raises(ValueError):
            decode({"type": "_Awaitable", "value": value, "binding": "job"})


@pytest.mark.parametrize(
    "type_name",
    ["_Run", "_Run<Text>", "_Run<Text", "_Run<Text>extra", "_Awaitable<Text>"],
)
def test_runtime_handle_output_rejects_legacy_and_suffixed_types(type_name):
    for decode in (output_from_data, output_from_protocol_data):
        with pytest.raises(ValueError):
            decode(
                {
                    "type": type_name,
                    "value": {
                        "kind": "run",
                        "id": "run_spawned",
                        "thread": "spawn_thread",
                        "result_type": "Text",
                    },
                    "binding": None,
                }
            )


@pytest.mark.parametrize("result_type", ["_Awaitable", "Text[", 3])
def test_awaitable_rejects_invalid_result_contract(result_type):
    for decode in (output_from_data, output_from_protocol_data):
        with pytest.raises(ValueError):
            decode(
                {
                    "type": "_Awaitable",
                    "value": {
                        "kind": "run",
                        "id": "run_spawned",
                        "thread": "spawn_thread",
                        "result_type": result_type,
                    },
                    "binding": None,
                }
            )


def test_stored_output_rejects_a_type_mismatched_with_its_value():
    with pytest.raises(ValueError, match="type does not match"):
        output_from_data({"type": "Number", "value": "text", "binding": None})
