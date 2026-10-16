"""Step payload serialization selects its variant without diagnostic expansion."""

import pytest
from pydantic import TypeAdapter

from toolang.base.types.message import Message
from toolang.base.types.run import ModelCall, ToolCall
from toolang.execution.types import ModelStepGiven, StepGiven, ToolStepGiven
from toolang.lang.ast import LetStmt, Span


_MODEL = ModelStepGiven(
    "test/model",
    ModelCall("", [Message.user("history " * 10_000)]),
    setup="setup-v1",
)
_TOOL = ToolStepGiven(
    "test",
    ToolCall("tool-1", "call-1", "test__execute", {"text": "input " * 10_000}),
)
_FLOW = LetStmt(value="Done", span=Span(line=1))


@pytest.mark.parametrize("given", [_MODEL, _TOOL], ids=["model", "tool"])
@pytest.mark.parametrize("mode", ["python", "json"])
def test_valid_step_payload_never_formats_a_rejected_union_branch(
    given, mode, monkeypatch
):
    adapter = TypeAdapter(StepGiven)
    concrete = TypeAdapter(type(given))
    expected = (
        concrete.dump_json(given) if mode == "json" else concrete.dump_python(given)
    )
    calls = []

    def diagnostic_repr(value):
        calls.append(type(value))
        return "unexpected diagnostic representation"

    monkeypatch.setattr(type(given), "__repr__", diagnostic_repr)
    actual = adapter.dump_json(given) if mode == "json" else adapter.dump_python(given)

    assert actual == expected
    assert calls == []


@pytest.mark.parametrize("given", [_MODEL, _TOOL, _FLOW], ids=["model", "tool", "flow"])
def test_step_payload_wire_format_round_trips_without_an_added_tag(given):
    adapter = TypeAdapter(StepGiven)
    encoded = adapter.dump_json(given)

    assert adapter.validate_json(encoded) == given
    assert adapter.dump_json(adapter.validate_json(encoded)) == encoded
