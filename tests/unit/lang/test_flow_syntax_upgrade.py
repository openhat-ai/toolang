"""Real-source contracts for the coordinated grammar upgrade."""

import pytest

from toolang.lang import Program, format_source, to_data
from toolang.lang.ast import (
    RepeatStmt,
    ScatterStmt,
    SettleStmt,
    Span,
    flow_stmt_from_data,
)
from toolang.lang.errors import ToolangError
from toolang.execution.settings import resolve_settings


@pytest.mark.parametrize("kind", ["agic", "flow"])
@pytest.mark.parametrize("layer", ["instruct", "context"])
def test_prompt_selection_only_requires_named_targets_to_exist(kind, layer):
    body = "user: Work." if kind == "agic" else "run: Work."
    for declaration in ("", f"{layer}: Module default.\n"):
        for value in ("default", "none"):
            Program.from_source(
                f"{declaration}{kind} work:\n  {layer} = {value}\n  {body}\n"
            )
    with pytest.raises(ToolangError, match=f"unknown {layer}"):
        Program.from_source(f"{kind} work:\n  {layer} = missing\n  {body}\n")


@pytest.mark.parametrize(
    "directive",
    [
        "hands = worker, none",
        "handoffs = none, worker",
        "recall = none, far",
        "lanes = 0",
        "lanes = -1",
        "context = none\n  context = default",
        "instruct = none\n  instruct = default",
        "lanes = 2\n  lanes = 4",
        "instruct: none",
        "context default",
        "recall = auto",
        "tools =",
    ],
)
def test_invalid_configuration_is_rejected_before_execution(directive):
    with pytest.raises(ToolangError):
        Program.from_source(f"agic work:\n  {directive}\n  user: Work.\n")


@pytest.mark.parametrize("tab_size", [1, 2, 4, 8])
@pytest.mark.parametrize("named", [True, False])
def test_settle_initializer_and_repeat_survive_formatting(tab_size, named):
    head = (
        "settle using merge:" if named else "settle:\n      Merge {{_}} with {{_1._}}."
    )
    source = f"""agic merge(_):
  Merge {{{{_}}}} with {{{{_1._}}}}.
flow work(_):
  lanes=default
  instruct=default
  repeat 5 times windowing 2:
    scatter:
      Split {{{{_}}}}.
    {head}
      from:
        Initial content.
    until: Compare {{{{_}}}} and {{{{_2._}}}}.
"""
    formatted = format_source(source, tab_size=tab_size)
    assert format_source(formatted, tab_size=tab_size) == formatted
    program = Program.from_source(formatted)
    loop = program.flows[0].stmts[0]
    assert isinstance(loop, RepeatStmt)
    assert loop.window == 2 and loop.count == 5
    settle = loop.stmts[1]
    assert isinstance(settle, SettleStmt)
    assert settle.initial == "Initial content."
    if not named:
        reducer = program.find_agic(settle.runnable)
        assert reducer is not None
        assert reducer.messages[0].content == "Merge {{_}} with {{_1._}}."


def test_legacy_scatter_records_decode_without_reintroducing_source_count():
    statement = ScatterStmt(span=Span(line=2), runnable="expand")
    encoded = to_data(statement)
    assert isinstance(encoded, dict) and "count" not in encoded
    legacy = {**encoded, "count": 4}
    assert flow_stmt_from_data(legacy) == statement
    for bad in (True, -1, "4", 4.5):
        with pytest.raises(ValueError):
            flow_stmt_from_data({**legacy, "count": bad})
    with pytest.raises(ToolangError):
        Program.from_source("flow work:\n  scatter 4 using expand\n")


@pytest.mark.parametrize("named", [True, False])
@pytest.mark.parametrize("tab_size", [2, 4])
def test_settle_comments_keep_their_authored_scope(named, tab_size):
    reducer = "" if named else "      Merge {{_}} with {{_1._}}.\n"
    source = (
        "agic merge(_):\n  Merge {{_}} with {{_1._}}.\n"
        "flow work(_):\n  repeat 2 times:\n    scatter: Items\n"
        f"    settle{' using merge' if named else ''}:\n"
        "      # Reducer or initializer comment.\n"
        f"{reducer}      from: Initial content.\n"
        "    # Next statement comment.\n"
        "    run: Continue {{_}}.\n"
    )
    formatted = format_source(source, tab_size=tab_size)
    assert " " * (3 * tab_size) + "# Reducer or initializer comment.\n" in formatted
    assert " " * (2 * tab_size) + "# Next statement comment.\n" in formatted
    assert format_source(formatted, tab_size=tab_size) == formatted
    Program.from_source(formatted)


@pytest.mark.parametrize("kind", ["agic", "flow"])
def test_lane_literals_use_the_same_integer_rules_as_statement_clauses(kind):
    body = "user: Work." if kind == "agic" else "storm 1 in 04 lanes using: Work."
    program = Program.from_source(f"{kind} work:\n  lanes = 04\n  {body}\n")
    runnable = program.agics[0] if kind == "agic" else program.flows[0]
    assert resolve_settings(runnable, "agent").lanes == 4
