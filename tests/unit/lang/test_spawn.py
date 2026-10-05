"""Spawn is a bindable launch statement, not an authored value type."""

import pytest

from toolang.lang import Program, format_source, to_data
from toolang.lang.ast import SpawnStmt, flow_stmt_from_data
from toolang.lang.errors import ToolangError
from toolang.lang.types import is_builtin_type, validate_struct_type


@pytest.mark.parametrize(
    "prefix,binding", [("", None), ("let ", None), ("let job = ", "job")]
)
@pytest.mark.parametrize(
    "target", ["worker", ": Work.", "-> Text[]: Work.", ":\n    Work."]
)
def test_spawn_forms_round_trip(prefix, binding, target):
    source = f"flow main():\n  {prefix}spawn {target}\nagic worker():\n  Work.\n"
    program = Program.from_source(source)
    statement = program.flows[0].stmts[0]
    assert isinstance(statement, SpawnStmt)
    assert statement.binding == binding
    assert flow_stmt_from_data(to_data(statement)) == statement
    formatted = format_source(source)
    assert format_source(formatted) == formatted
    assert "let spawn" not in formatted
    assert Program.from_source(formatted).flows[0].stmts[0].binding == binding


def test_spawn_keyword_takes_priority_and_run_is_not_a_type():
    with pytest.raises(ToolangError):
        Program.from_source("flow main:\n  let text = spawn a process\n")
    assert not is_builtin_type("Run")
    assert validate_struct_type("Run") == "Run"


def test_handle_is_not_the_target_result_or_a_normal_input():
    with pytest.raises(ToolangError, match="handle.*cannot be passed"):
        Program.from_source("""
flow main():
  let job = spawn worker
  run consumer
agic worker() -> Text:
  Work.
agic consumer(job: Text):
  {{job}}
""")


@pytest.mark.parametrize(
    "template",
    ["{{job.result}}", "{{job.status.extra}}", "{{#job}}{{missing}}{{/job}}"],
)
@pytest.mark.parametrize("statement", ["let detail = ", "run: "])
def test_handle_fields_are_checked_without_a_language_type(template, statement):
    with pytest.raises(Exception, match="unknown template field"):
        Program.from_source(f"""
flow parent(_: Text) -> Text:
  let job = spawn child
  {statement}{template}
flow child(_: Text) -> Text:
  let unused = Work
""")
