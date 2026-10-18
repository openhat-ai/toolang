"""Step payload serialization selects its variant without diagnostic expansion."""

import pytest
from pydantic import TypeAdapter

from toolang.base.types.message import Message
from toolang.base.types.run import ModelCall, ToolCall
from toolang.execution.types import ModelStepSpec, StepSpec, ToolStepSpec
from toolang.lang.ast import LetStmt, Span


_MODEL = ModelStepSpec(
    "test/model",
    ModelCall("", [Message.user("history " * 10_000)]),
    setup="setup-v1",
)
_TOOL = ToolStepSpec(
    "test",
    ToolCall("tool-1", "call-1", "test__execute", {"text": "input " * 10_000}),
)
_FLOW = LetStmt(value="Done", span=Span(line=1))


@pytest.mark.parametrize("spec", [_MODEL, _TOOL], ids=["model", "tool"])
@pytest.mark.parametrize("mode", ["python", "json"])
def test_valid_step_payload_never_formats_a_rejected_union_branch(
    spec, mode, monkeypatch
):
    adapter = TypeAdapter(StepSpec)
    concrete = TypeAdapter(type(spec))
    expected = (
        concrete.dump_json(spec) if mode == "json" else concrete.dump_python(spec)
    )
    calls = []

    def diagnostic_repr(value):
        calls.append(type(value))
        return "unexpected diagnostic representation"

    monkeypatch.setattr(type(spec), "__repr__", diagnostic_repr)
    actual = adapter.dump_json(spec) if mode == "json" else adapter.dump_python(spec)

    assert actual == expected
    assert calls == []


@pytest.mark.parametrize("spec", [_MODEL, _TOOL, _FLOW], ids=["model", "tool", "flow"])
def test_step_payload_wire_format_round_trips_without_an_added_tag(spec):
    adapter = TypeAdapter(StepSpec)
    encoded = adapter.dump_json(spec)

    assert adapter.validate_json(encoded) == spec
    assert adapter.dump_json(adapter.validate_json(encoded)) == encoded
