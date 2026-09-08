"""Output bindings are independent of reusable local values and dimensions."""

from contextlib import closing
from dataclasses import FrozenInstanceError
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
    Local,
    Output,
    Pointer,
    StepRef,
    TypedRef,
    local_to_protocol_data,
    output_from_protocol_data,
    output_to_protocol_data,
)
from toolang.lang.types import Array


@pytest.mark.parametrize("binding", [None, "_", "answer"])
@pytest.mark.parametrize(
    "local",
    [
        Local(value="", dim=0),
        Local(value=0, dim=0),
        Local(value=False, dim=0),
        Local(value=None, dim=0),
        Local(value=Array("Part[]", (TextPart("answer"),)), dim=0),
        Local(value=Array("Text[]", ()), dim=0),
        Local(value=Array("Text[]", ()), dim=1),
        Local(
            value=TypedRef(
                FieldRef.from_path(
                    StepRef.parse("run_source.0"), "output", "local", "value"
                ),
                "Text[]",
            ),
            dim=1,
        ),
    ],
)
def test_output_reuses_local_across_storage_and_protocol(
    local: Local, binding: str | None
) -> None:
    locals = {"answer": local}
    output = Output(local=locals["answer"], binding=binding)
    assert output.local is local
    assert not hasattr(local, "name")
    assert output_from_data(output_to_data(output)) == output
    data = output_to_protocol_data(output)
    assert data == {"local": local_to_protocol_data(local), "binding": binding}
    assert output_from_protocol_data(data) == output
    adapter = TypeAdapter(Output)
    assert adapter.dump_python(output, mode="json") == data
    assert adapter.validate_json(adapter.dump_json(output)) == output
    event = RunEnd(run="run_result", status="succeeded", output=output)
    assert run_event_from_data(run_event_to_data(event)) == event


def test_output_binding_is_validated_and_immutable() -> None:
    output = Output(local=Local(value="result", dim=0), binding="_")
    with pytest.raises(FrozenInstanceError):
        setattr(output, "binding", "answer")
    for binding in ("", "bad name", "bad-name", 1, False):
        with pytest.raises(ValueError, match="invalid output binding"):
            Output(local=output.local, binding=binding)  # ty: ignore[invalid-argument-type]


@pytest.mark.parametrize("binding", [None, "_", "answer"])
@pytest.mark.parametrize(
    "local",
    [None, Local(value=None, dim=0), Local(value=Array("Text[]", ("a", "b")), dim=1)],
)
def test_output_presence_bindings_and_references_survive_reopening(
    tmp_path: Path, local: Local | None, binding: str | None
) -> None:
    path = tmp_path / "runs.db"
    output = Output(local=local, binding=binding) if local is not None else None
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
        reference = FieldRef.from_path(step.ref, "output", "local", "value")
        retained = (
            Output(
                local=Local(value=TypedRef(reference, local.type), dim=local.dim),
                binding=binding,
            )
            if local is not None
            else None
        )
        project_run_end(store, run_id=run.id, output=retained)

    with closing(RunStore(path)) as store:
        restored = store.get_step(ref=step.ref)
        final = store.get_run(run_id=run.id)
        assert restored is not None and final is not None
        assert restored.output == output
        assert final.output == retained
        if local is None:
            assert final.output is None
        else:
            assert final.output is not None
            assert store.resolve_output(final.output) == output
            assert store.select_pointer(Pointer(reference)).runtime == local.value
            assert (
                store.select_pointer(
                    Pointer(FieldRef.from_path(step.ref, "output", "local"))
                ).runtime
                == local
            )
            assert (
                store.select_pointer(
                    Pointer(FieldRef.from_path(step.ref, "output", "binding"))
                ).runtime
                == binding
            )
            with pytest.raises(ValueError, match="field does not exist"):
                store.select_pointer(
                    Pointer(FieldRef.from_path(step.ref, "output", "value"))
                )


@pytest.mark.parametrize(
    "data",
    [
        {"value": "old", "name": "_", "dim": 0},
        {"value": {"value": "old", "dim": 0}, "binding": "_"},
        {"local": None, "binding": "_"},
    ],
)
def test_output_codecs_reject_old_or_missing_local_shapes(data) -> None:
    for decode in (output_from_data, output_from_protocol_data):
        with pytest.raises(ValueError):
            decode(data)
