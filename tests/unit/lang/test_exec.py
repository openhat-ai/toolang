"""Named and inline exec share run target lowering and validation."""

import pytest

from toolang.lang import Program, format_source, to_data
from toolang.lang.ast import ExecStmt, RunStmt, flow_stmt_from_data
from toolang.lang.errors import ToolangError


@pytest.mark.parametrize(
    "statement",
    [
        "exec successor",
        "exec: Complete the task.",
        "exec -> Text:\n    Complete the task.",
        "exec -> Text[]: Return the results.",
    ],
)
def test_exec_round_trip_and_target_parity(statement):
    source = f"flow grow():\n  {statement}\nagic successor():\n  Work.\n"
    formatted = format_source(source)
    assert format_source(formatted) == formatted
    program = Program.from_source(formatted)
    stmt = program.flows[0].stmts[0]
    assert isinstance(stmt, ExecStmt)
    assert stmt.binding is None
    assert flow_stmt_from_data(to_data(stmt)) == stmt
    run = Program.from_source(formatted.replace("exec", "run", 1))
    run_stmt = run.flows[0].stmts[0]
    assert isinstance(run_stmt, RunStmt)
    assert stmt.runnable == run_stmt.runnable
    assert program.agics == run.agics


@pytest.mark.parametrize(
    "statement",
    [
        "exec",
        "exec foo bar",
        "exec foo()",
        "exec foo:",
        "let x = exec foo",
        "let exec foo",
        "exec missing",
    ],
)
def test_invalid_exec_has_source_diagnostic(statement):
    with pytest.raises(ToolangError):
        Program.from_source(f"flow grow():\n  {statement}\nagic foo():\n  Work.\n")


def test_exec_stops_input_analysis_through_repeat_boundaries():
    Program.from_source("""
flow grow():
  repeat 2 times:
    repeat 3 times:
      exec successor
    until: This cannot read {{missing}}.
  run: This cannot read {{missing}} either.
agic successor():
  Work.
""")
