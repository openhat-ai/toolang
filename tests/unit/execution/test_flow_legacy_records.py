"""Old records remain inspectable without enabling obsolete execution syntax."""

from typing import Any, cast
import json

import pytest
from pydantic import TypeAdapter

from toolang.execution.inspection.types import historical_statement_head
from toolang.execution.records import (
    local_from_data,
    local_to_data,
    recorded_flow_stmt_from_data,
    step_given_from_data,
    step_given_to_data,
)
from toolang.execution.types import (
    HistoricalFlowStmt,
    Local,
    StepGiven,
    local_from_protocol_data,
    local_to_protocol_data,
)
from toolang.lang import to_data
from toolang.lang.ast import (
    RunStmt,
    GenerateStmt,
    ReduceStmt,
    RepeatStmt,
    Span,
    flow_stmt_from_data,
    program_from_data,
)
from toolang.lang.types import Array


@pytest.mark.parametrize(
    "kind,step_kind,current",
    [
        ("scatter", "run", RunStmt(span=Span(line=2), runnable="expand")),
        ("gather", "run", RunStmt(span=Span(line=2), runnable="merge")),
        (
            "storm",
            "par",
            GenerateStmt(span=Span(line=2), runnable="worker", count=2, lanes=1),
        ),
        (
            "settle",
            "loop",
            ReduceStmt(span=Span(line=2), runnable="merge", initial="seed"),
        ),
    ],
)
def test_historical_steps_round_trip_and_remain_inspectable(kind, step_kind, current):
    raw = cast(dict[str, Any], to_data(current))
    assert isinstance(raw, dict)
    raw["kind"] = kind
    if kind == "scatter":
        raw["count"] = 4
    statement = step_given_from_data(step_kind, raw)
    assert isinstance(statement, HistoricalFlowStmt)
    assert historical_statement_head(statement).startswith(kind)
    assert step_given_to_data(step_kind, statement) == raw
    adapter = TypeAdapter(StepGiven)
    assert adapter.dump_python(adapter.validate_python(raw), mode="json") == raw
    for parse in (flow_stmt_from_data, program_from_data):
        with pytest.raises(ValueError, match="migrate source"):
            parse(raw)


@pytest.mark.parametrize("count", [-1, True, "4", 4.5])
def test_legacy_scatter_count_retains_its_old_validation(count):
    raw = to_data(RunStmt(span=Span(line=2), runnable="expand"))
    assert isinstance(raw, dict)
    with pytest.raises(ValueError, match="non-negative integer"):
        recorded_flow_stmt_from_data({**raw, "kind": "scatter", "count": count})


def test_repeat_containing_old_syntax_is_historical_as_a_whole():
    child = RunStmt(span=Span(line=3), runnable="expand")
    raw = cast(
        dict[str, Any], to_data(RepeatStmt(span=Span(line=2), count=2, stmts=(child,)))
    )
    assert isinstance(raw, dict)
    raw["stmts"][0]["kind"] = "scatter"
    statement = step_given_from_data("loop", raw)
    assert isinstance(statement, HistoricalFlowStmt)
    assert step_given_to_data("loop", statement) == raw
    with pytest.raises(ValueError, match="migrate source"):
        flow_stmt_from_data(raw)


@pytest.mark.parametrize("dim", [0, 1])
def test_legacy_local_dimension_is_read_but_never_written(dim):
    local = Local(Array("Text[][]", (Array("Text[]", ("a", "b")),)))
    current = local_to_data(local)
    assert set(current) == {"value"}
    assert local_from_data({**current, "dim": dim}) == local
    protocol = local_to_protocol_data(local)
    assert set(protocol) == {"type", "value"}
    assert local_from_protocol_data(protocol) == local
    with pytest.raises(ValueError):
        local_from_protocol_data({**protocol, "dim": dim})
    with pytest.raises(ValueError):
        TypeAdapter(Local).validate_json(json.dumps({**protocol, "dim": dim}))


@pytest.mark.parametrize("dim", [False, True, -1, 2, "1", None])
def test_invalid_legacy_dimension_is_rejected(dim):
    with pytest.raises(ValueError, match="dim"):
        local_from_data({"value": "value", "dim": dim})
