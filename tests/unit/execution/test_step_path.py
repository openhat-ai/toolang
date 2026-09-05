from __future__ import annotations

import pytest
from pydantic import TypeAdapter

from toolang.execution.types import (
    ContentRef,
    ControlRef,
    FieldRef,
    JsonPointer,
    Pointer,
    PointerType,
    RunRef,
    StepPath,
    StepRef,
    ThreadRef,
    TypedRef,
)


def test_step_ref_round_trips_global_and_local_forms() -> None:
    ref = StepRef.parse("run_root.2.3")

    assert ref.run == RunRef("run_root")
    assert ref.path == StepPath((2, 3))
    assert ref.indices == (2, 3)
    assert ref.local == "2.3"
    assert ref.index == 3
    assert ref.parent == StepRef.parse("run_root.2")
    assert ref.child(4) == StepRef.parse("run_root.2.3.4")
    assert StepRef.from_local("run_root", "2.3") == ref
    assert TypeAdapter(StepRef).dump_json(ref) == b'"run_root.2.3"'


def test_step_path_is_run_relative() -> None:
    path = StepPath.parse("2.3")

    assert path.indices == (2, 3)
    assert path.parent == StepPath((2,))
    assert path.child(4) == StepPath((2, 3, 4))
    assert str(path) == "2.3"


@pytest.mark.parametrize(
    "value",
    (
        "",
        "run_root",
        "run_root.",
        ".0",
        "run_root.-1",
        "run_root.01",
        "run_root/0",
        "run_root.0/1",
    ),
)
def test_step_ref_rejects_noncanonical_values(value: str) -> None:
    with pytest.raises(ValueError):
        StepRef.parse(value)


def test_every_reference_kind_round_trips_through_pointer() -> None:
    thread = ThreadRef("term_root")
    run = RunRef("run_root")
    step = StepRef(run, StepPath((2, 3)))
    run_control = ControlRef(run, 4)
    thread_control = ControlRef(thread, 5)
    content = ContentRef("sha256_" + "a" * 64)
    field = FieldRef(step, JsonPointer("/output/value/0"))
    typed = TypedRef(field, "Part")
    cases = (
        (thread, PointerType.THREAD_REF),
        (run, PointerType.RUN_REF),
        (step, PointerType.STEP_REF),
        (run_control, PointerType.CONTROL_REF),
        (thread_control, PointerType.CONTROL_REF),
        (content, PointerType.CONTENT_REF),
        (field, PointerType.FIELD_REF),
        (typed, PointerType.TYPED_REF),
    )

    for ref, expected_type in cases:
        pointer = Pointer(ref)
        assert Pointer.parse(str(pointer)) == pointer
        assert pointer.ref() == ref
        assert pointer.type == expected_type

    assert Pointer(thread).thread_ref() == thread
    assert Pointer(run).run_ref() == run
    assert Pointer(step).step_ref() == step
    assert Pointer(run_control).control_ref() == run_control
    assert Pointer(content).content_ref() == content
    assert Pointer(field).field_ref() == field
    assert Pointer(typed).typed_ref() == typed
    assert Pointer(run).thread_ref() is None


@pytest.mark.parametrize(
    "value",
    (
        "sha256_short",
        "sha256_" + "A" * 64,
        "run_root@01",
        "run_root.-1",
        "run_root/output~2value",
        "run_root:Text",
        "run_root/output:Not A Type",
    ),
)
def test_pointer_rejects_malformed_reserved_forms(value: str) -> None:
    with pytest.raises(ValueError):
        Pointer.parse(value)


def test_concrete_references_reject_wrong_component_kinds() -> None:
    with pytest.raises(ValueError, match="invalid thread ref"):
        ThreadRef("sha256_custom")
    with pytest.raises(TypeError, match="RunRef"):
        StepRef(ThreadRef("term_root"), StepPath((0,)))  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="ThreadRef or RunRef"):
        ControlRef(StepRef.parse("run_root.0"), 0)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="RecordRef"):
        FieldRef("run_root", JsonPointer("/output"))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="invalid field ref"):
        TypedRef.parse("run_root:Text")


@pytest.mark.parametrize("value", ((True,), (1, False), ("1",)))
def test_step_path_rejects_non_integer_indexes(value: tuple[object, ...]) -> None:
    with pytest.raises(ValueError, match="non-negative indices"):
        StepPath(value)  # type: ignore[arg-type]


def test_control_ref_rejects_boolean_index() -> None:
    with pytest.raises(TypeError, match="integer"):
        ControlRef(RunRef("run_root"), True)
