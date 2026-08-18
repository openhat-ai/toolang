from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest

from tests import FIXTURES_ROOT, PROJECT_ROOT
from toolang.common.errors import ToolangError
from toolang.lang import Program, to_data
from toolang.lang.ast import LetStmt, RepeatStmt, SettleStmt
from toolang.base.types.message import TextPart
from toolang.lang.input import resolve_input_parts
from toolang.state.source import read_authored_source


def test_program_lowers_declarations_to_static_nodes() -> None:
    program = Program.from_source(
        """
with skill briceyan/review

service github:
  description = Use GitHub.
  protocol = http
  target = https://mcp.github.com/mcp

  Connect to GitHub.

prompt review:
  params = path, focus?

  Review {{path}}.
  {{focus}}

struct Review:
  title: Text
  body?: Text

context:
  Shared context.

instruct concise:
  Be concise.

agic review(_: Text, focus?) -> Review:
  context: default
  instruct: concise
  user: Review {{_}}.

flow main:
  run review
"""
    )

    assert [(item.cap_kind, item.reference) for item in program.withs] == [
        ("skill", "briceyan/review")
    ]
    service, prompt = program.caps
    assert service.kind == "service"
    assert service.meta["target"] == "https://mcp.github.com/mcp"
    with pytest.raises(TypeError):
        cast(Any, service.meta)["target"] = "https://example.com"
    assert [(item.name, item.optional) for item in prompt.params] == [
        ("path", False),
        ("focus", True),
    ]
    assert [(item.name, item.optional) for item in program.structs[0].fields] == [
        ("title", False),
        ("body", True),
    ]
    assert program.contexts[0].name == "default"
    assert program.instructs[0].name == "concise"
    agic = program.agics[0]
    assert agic.name == "review"
    assert agic.context == "default"
    assert agic.instruct == "concise"
    assert agic.messages[0].role == "user"
    assert agic.messages[0].content == "Review {{_}}."
    assert program.flows[0].name == "main"
    assert program.flows[0].stmts[0].kind == "run"


def test_parameters_distinguish_implicit_empty_and_explicit_input() -> None:
    program = Program.from_source(
        """
agic implicit:
  Hello.

agic empty():
  Hello.

agic explicit(_):
  Hello.

agic args(name: Text, detail?):
  Hello.

agic custom(_: Json, detail: Text):
  Hello.
"""
    )

    implicit, empty, explicit, args, custom = program.agics
    assert implicit.input is not None
    assert (implicit.input.name, implicit.input.type_name) == ("_", "Part[]")
    assert empty.input is None and empty.params == ()
    assert explicit.input is not None
    assert (explicit.input.name, explicit.input.type_name) == ("_", "Part[]")
    assert args.input is None
    assert [(item.name, item.type_name, item.optional) for item in args.params] == [
        ("name", "Text", False),
        ("detail", None, True),
    ]
    assert custom.input is not None and custom.input.type_name == "Json"
    assert [(item.name, item.type_name) for item in custom.params] == [
        ("detail", "Text")
    ]


@pytest.mark.parametrize(
    ("signature", "message"),
    [
        ("_?", "must not be optional"),
        ("name, _: Json", "must be the first parameter"),
    ],
)
def test_primary_input_validation(signature: str, message: str) -> None:
    with pytest.raises(ToolangError, match=message):
        Program.from_source(f"agic invalid({signature}):\n  pass\n")


def test_program_lowers_complete_flow_statement_set() -> None:
    program = Program.from_source(
        """
agic action:
  pass

agic predicate -> Boolean:
  pass

agic score -> Number:
  pass

flow pipeline:
  run action
  seek reviewer action
  ask: Continue?
  scatter 2 action
  storm 3 action par 2
  gather action
  settle action
  map action par 4
  keep first 2
  keep predicate par 2
  drop last 1
  drop predicate
  rank score top 3 par 2
  let saved = run action
  let note:
    Store this note.

  repeat 2:
    run action
    until: Complete?
"""
    )

    statements = program.flows[0].stmts
    assert [statement.kind for statement in statements] == [
        "run",
        "seek",
        "ask",
        "scatter",
        "storm",
        "gather",
        "settle",
        "map",
        "keep",
        "keep",
        "drop",
        "drop",
        "rank",
        "run",
        "let",
        "repeat",
    ]
    assert statements[13].binding == "saved"
    let_statement = statements[14]
    assert isinstance(let_statement, LetStmt)
    assert let_statement.binding == "note"
    assert let_statement.value == "Store this note."
    repeat = statements[-1]
    assert isinstance(repeat, RepeatStmt)
    assert repeat.count == 2
    assert [statement.kind for statement in repeat.stmts] == ["run"]
    assert repeat.until is not None


def test_inline_settle_exposes_the_current_item() -> None:
    program = Program.from_source(
        """
flow summarize(_: Text[]) -> Text:
  settle -> Text:
    {{_}}{{item}}
"""
    )

    statement = program.flows[0].stmts[0]
    assert isinstance(statement, SettleStmt)
    generated = next(agic for agic in program.agics if agic.name == statement.runnable)
    assert generated.input is not None
    assert generated.input.type_name == "Part[]"
    assert [(param.name, param.type_name) for param in generated.params] == [
        ("item", "Part[]")
    ]


def test_inline_flow_evaluators_disable_recall_and_tools() -> None:
    program = Program.from_source(
        """
agic action:
  pass

flow evaluate:
  keep: Return true when the item is useful.
  rank: Return a relevance score.
  repeat 2:
    run action
    until: Return true when complete.
"""
    )

    generated = [agic for agic in program.agics if agic.name.startswith("<agic:")]

    assert sorted(agic.output for agic in generated if agic.output) == [
        "Boolean",
        "Boolean",
        "Number",
    ]
    for agic in generated:
        assert [
            (directive.name, directive.operator, directive.values)
            for directive in agic.directives
        ] == [
            ("recall", "=", ("none",)),
            ("tools", "=", ("none",)),
        ]


def test_inline_prompting_is_flattened_and_referenced() -> None:
    program = Program.from_source(
        """
agic answer:
  context:
    Local context.
  instruct:
    Local instruct.
  Answer.
"""
    )
    agic = program.agics[0]

    assert agic.context == "<context:3>"
    assert agic.instruct == "<instruct:5>"
    assert [(item.name, item.body) for item in program.contexts] == [
        ("<context:3>", "Local context.")
    ]
    assert [(item.name, item.body) for item in program.instructs] == [
        ("<instruct:5>", "Local instruct.")
    ]


def test_directives_use_canonical_names() -> None:
    program = Program.from_source(
        """
agic configured:
  models = openai/gpt-5
  tools += shell
  skills -= review

  Configured.
"""
    )

    assert [item.name for item in program.agics[0].directives] == [
        "models",
        "tools",
        "skills",
    ]


def test_to_data_uses_node_kinds_and_span_objects() -> None:
    data = cast(
        dict[str, object],
        to_data(Program.from_source("agic hello:\n  Hello.\n")),
    )

    assert data["kind"] == "program"
    assert data["span"] == {"line": 1}
    agics = cast(list[dict[str, object]], data["agics"])
    assert agics[0]["kind"] == "agic"
    assert agics[0]["span"] == {"line": 1}


def test_program_data_round_trips_without_parsing_source() -> None:
    from toolang.lang.ast import program_from_data

    program = Program.from_source(
        "agic hello:\n  Hello.\n\nflow work:\n  repeat 2:\n    run hello\n"
    )

    assert program_from_data(to_data(program)) == program


def test_program_data_round_trip_preserves_ambiguous_statement_kinds() -> None:
    from toolang.lang.ast import program_from_data

    program = Program.from_source(
        """
agic action:
  pass

agic predicate -> Boolean:
  pass

flow work:
  gather action
  settle action
  keep predicate
  drop predicate
  repeat 2:
    settle action
    drop predicate
"""
    )

    restored = program_from_data(to_data(program))

    assert restored == program
    assert [type(statement) for statement in restored.flows[0].stmts] == [
        type(statement) for statement in program.flows[0].stmts
    ]
    restored_repeat = restored.flows[0].stmts[-1]
    original_repeat = program.flows[0].stmts[-1]
    assert isinstance(restored_repeat, RepeatStmt)
    assert isinstance(original_repeat, RepeatStmt)
    assert [type(statement) for statement in restored_repeat.stmts] == [
        type(statement) for statement in original_repeat.stmts
    ]


def test_program_data_rejects_unknown_statement_kind() -> None:
    from toolang.lang.ast import program_from_data

    data = cast(
        dict[str, Any],
        to_data(
            Program.from_source("agic action:\n  pass\nflow work:\n  run action\n")
        ),
    )
    flows = cast(list[dict[str, Any]], data["flows"])
    flows[0]["stmts"][0]["kind"] = "future"

    with pytest.raises(ValueError, match="Input tag 'future'"):
        program_from_data(data)


def test_validation_runs_after_lowering() -> None:
    with pytest.raises(ToolangError, match="missing description"):
        Program.from_source(
            """
service github:
  protocol = http
  target = https://mcp.github.com/mcp
"""
        )

    with pytest.raises(ToolangError, match="conflicts with a built-in type"):
        Program.from_source(
            """
struct Json:
  value: Text
"""
        )

    with pytest.raises(ToolangError, match="Duplicate runnable name"):
        Program.from_source(
            """
agic same:
  Hello.

flow same:
  run same
"""
        )


def test_program_find_methods_do_not_infer_defaults() -> None:
    program = Program.from_source(
        """
context repo:
  Repository context.

instruct concise:
  Be concise.

agic review:
  Review it.

flow pipeline:
  run review
"""
    )

    assert program.find_agic("review") is program.agics[0]
    assert program.find_flow("pipeline") is program.flows[0]
    assert program.find_context("repo") is program.contexts[0]
    assert program.find_instruct("concise") is program.instructs[0]
    assert program.find_agic("default") is None
    assert program.find_flow("main") is None
    assert program.find_context("default") is None
    assert program.find_instruct("default") is None


def test_program_source_hides_header_without_adding_runtime_declarations(
    tmp_path: Path,
) -> None:
    root = tmp_path / "toolang"
    agent_dir = root / "agents" / "alice"
    agent_dir.mkdir(parents=True)
    (agent_dir / "agent.too").write_text(
        "#!/usr/bin/env toolang\n\nagent alice\n",
        encoding="utf-8",
    )

    prepared = read_authored_source(root, "alice").load_program()
    program = prepared.parse()

    assert prepared.source_text == "#!/usr/bin/env toolang\n\nagent alice\n"
    assert program.agics == ()


def test_program_source_parse_preserves_authored_shebang_line_numbers(
    tmp_path: Path,
) -> None:
    root = tmp_path / "toolang"
    agent_dir = root / "agents" / "alice"
    agent_dir.mkdir(parents=True)
    (agent_dir / "agent.too").write_text(
        "#!/usr/bin/env toolang\n\n# Agent description.\nagent alice\n\nagic chat:\n  Reply.\n",
        encoding="utf-8",
    )

    source = read_authored_source(root, "alice").load_program()
    program = source.parse()

    assert source.source_text.startswith("#!/usr/bin/env toolang\n")
    assert program.agics[0].span.line == 6


def test_program_source_preserves_explicit_default_agic(tmp_path: Path) -> None:
    root = _write_program(
        tmp_path,
        """
agic:
  Reply directly.
""".strip(),
    )

    program = read_authored_source(root, "alice").load_program().parse()

    assert len(program.agics) == 1
    assert program.agics[0].name == "default"
    assert program.agics[0].messages[0].content == "Reply directly."


def test_program_expands_prompt_calls(tmp_path: Path) -> None:
    root = _write_program(
        tmp_path,
        """
prompt review:
  params = path, focus?

  Review {{path}} carefully.
  {{focus}}

  {{_}}

agic:
  Respond directly.
""".strip(),
    )
    program = read_authored_source(root, "alice").load_program().parse()

    expanded = resolve_input_parts(
        '/review path=src/app.py focus="only errors" -\n\nAlso inspect tests.',
        program=program,
    )

    assert expanded == (
        TextPart("Review src/app.py carefully.\nonly errors\n\n\nAlso inspect tests."),
    )


def test_repo_program_fixtures_parse_cleanly() -> None:
    for source_path in sorted(FIXTURES_ROOT.glob("*.too")):
        program = Program.from_source(source_path.read_text(encoding="utf-8"))
        assert program.agics, source_path.name


def test_example_programs_parse_cleanly() -> None:
    for source_path in sorted((PROJECT_ROOT / "examples").glob("*.too")):
        if source_path.name == "script-playground.too":
            continue
        program = Program.from_source(source_path.read_text(encoding="utf-8"))
        assert program.agics, source_path.name


def _write_program(tmp_path: Path, body_text: str) -> Path:
    root = tmp_path / "toolang"
    agent_dir = root / "agents" / "alice"
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "agent.too").write_text(
        f"agent alice\n\n{body_text}\n",
        encoding="utf-8",
    )
    return root
