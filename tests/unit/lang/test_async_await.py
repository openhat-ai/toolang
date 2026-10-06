"""Phase-one syntax, trusted operands, and binding contracts."""

import pytest

from toolang.lang import Program, format_source, format_statement_head, to_data
from toolang.lang.ast import AwaitStmt, RunStmt, flow_stmt_from_data
from toolang.lang.errors import ToolangError


@pytest.mark.parametrize(
    "target", ["worker", ": Inline prompt", "-> Text: Typed inline prompt"]
)
@pytest.mark.parametrize("binding", ["", "let ", "let job = "])
def test_async_run_roundtrip_and_binding(target, binding):
    source = (
        f"agic worker():\n  user: Work\nflow main():\n  {binding}async run {target}\n"
    )
    program = Program.from_source(source)
    stmt = program.flows[0].stmts[0]
    assert isinstance(stmt, RunStmt) and stmt.asynchronous
    assert stmt.binding == ("job" if binding == "let job = " else None)
    assert flow_stmt_from_data(to_data(stmt)) == stmt
    formatted = format_source(source)
    reparsed = Program.from_source(formatted)
    assert format_source(formatted) == formatted
    assert [(s.kind, s.binding) for s in reparsed.flows[0].stmts] == [
        (s.kind, s.binding) for s in program.flows[0].stmts
    ]
    assert "async run" in format_statement_head(stmt)


@pytest.mark.parametrize(
    "wait,destination",
    [
        ("await job", "_"),
        ("let result = await job", "result"),
        ("let await job", None),
        ("let job = await job", "job"),
    ],
)
@pytest.mark.parametrize("launch", ["async run", "spawn"])
def test_await_binding_is_separate_from_operand(wait, destination, launch):
    source = f"agic worker():\n  user: Work\nflow main():\n  let job = {launch} worker\n  {wait}\n"
    program = Program.from_source(source)
    stmt = program.flows[0].stmts[1]
    assert (
        isinstance(stmt, AwaitStmt)
        and stmt.handle == "job"
        and stmt.binding == destination
    )
    assert flow_stmt_from_data(to_data(stmt)) == stmt
    formatted = format_source(source)
    reparsed = Program.from_source(formatted)
    assert format_source(formatted) == formatted
    assert [(s.kind, s.binding) for s in reparsed.flows[0].stmts] == [
        (s.kind, s.binding) for s in program.flows[0].stmts
    ]
    assert format_statement_head(stmt) == wait


@pytest.mark.parametrize(
    "statement",
    [
        "await missing",
        "await _",
        "await job, job",
        "await [job]",
        "await spawn worker",
        "await:",
        "async map worker",
        "async repeat 2 times:",
        "async:",
    ],
)
def test_unsupported_operands_and_phase_two_forms_are_rejected(statement):
    with pytest.raises(ToolangError):
        Program.from_source(
            f"agic worker():\n  user: Work\nflow main():\n  let job = async run worker\n  {statement}\n"
        )


def test_metadata_and_json_do_not_acquire_handle_semantics():
    with pytest.raises(ToolangError, match="retained handle"):
        Program.from_source(
            'flow main():\n  let fake = {"kind":"run","id":"run_fake"}\n  await fake\n'
        )
