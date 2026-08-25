from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError
import pytest

from tests.support.execution_fixtures import project_run_start, project_step
from toolang.base.types.message import Message, TextPart
from toolang.execution.records import ControlRecord, RunRecord, StepRecord, ThreadRecord
from toolang.execution.schemas import (
    record_from_data,
    record_schema,
    record_to_data,
    select_json_field,
)
from toolang.execution.store import RunStore
from toolang.execution.types import Local, Pointer
from toolang.lang.types import Array


def test_field_refs_select_escaped_and_empty_object_members() -> None:
    document = {"a/b": {"~": ["first"]}, "": "empty"}

    escaped = Pointer("term_fields/a~1b/~0/0")
    empty = Pointer("term_fields/")

    assert (
        select_json_field(
            document,
            escaped.field_tokens,
            source=str(escaped),
        )
        == "first"
    )
    assert (
        select_json_field(
            document,
            empty.field_tokens,
            source=str(empty),
        )
        == "empty"
    )


@pytest.mark.parametrize("token", ("01", "-", "missing"))
def test_field_refs_reject_invalid_or_missing_array_members(token: str) -> None:
    with pytest.raises(ValueError):
        select_json_field(["first"], (token,), source=f"term_fields/{token}")


def test_store_resolves_records_and_fields_without_following_selected_references(
    tmp_path: Path,
) -> None:
    store = RunStore(tmp_path / "runs.db")
    try:
        run = project_run_start(
            store,
            run_id="run_fields",
            thread_id="term_fields",
            origin="test",
            input=Message.user("source"),
        )
        source = Pointer.control(
            run.id,
            0,
            "payload",
            "locals",
            0,
            "value",
        )
        step = project_step(
            store,
            run_id=run.id,
            step_index=0,
            kind="value",
            status="succeeded",
            input=(source,),
            output=Local.typed("Part[]", source, "_"),
            started_at="2026-01-01T00:00:00Z",
            finished_at="2026-01-01T00:00:01Z",
        )

        assert isinstance(store.resolve_pointer(Pointer("term_fields")), ThreadRecord)
        assert isinstance(store.resolve_pointer(Pointer("run_fields^0")), ControlRecord)
        assert isinstance(store.resolve_pointer(Pointer("run_fields")), RunRecord)
        assert isinstance(store.resolve_pointer(Pointer("run_fields.0")), StepRecord)
        assert store.resolve_pointer(Pointer("run_fields/status")) == "running"
        assert store.resolve_pointer(Pointer("run_fields/finished_at")) is None
        assert store.resolve_pointer(Pointer("run_fields.0/output/value")) == {
            "?": f"@{source}"
        }
        assert step.output is not None
        assert store.resolve_value(step.output.value) == Array(
            "Part[]",
            (TextPart("source"),),
        )
        assert (
            store.dereference_value_pointer(
                Pointer("run_fields.0/output/value"),
                "Part[]",
            )
            == source
        )
    finally:
        store.close()


def test_record_schema_is_strict_and_describes_error_pointers(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs.db")
    try:
        run = project_run_start(
            store,
            run_id="run_error",
            thread_id="term_error",
            origin="test",
            input=Message.user("fail"),
        )
    finally:
        store.close()
    failed = replace(
        run,
        status="failed",
        error=Pointer("run_error.0/error"),
    )
    data = record_to_data(failed)
    schema = record_schema("run")
    validator = Draft202012Validator(schema)

    validator.validate(data)
    assert record_from_data("run", data) == failed
    with pytest.raises(ValidationError):
        validator.validate(
            {name: value for name, value in data.items() if name != "id"}
        )
    with pytest.raises(ValidationError):
        validator.validate({**data, "summary": "synthetic"})
