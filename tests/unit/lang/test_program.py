from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest

from tests import FIXTURES_ROOT, PROJECT_ROOT
from toolang.common.error import ToolangError
from toolang.lang import Program, to_data
from toolang.state.durable import scan_durable_state
from toolang.lang.source import expand_program_input


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

agic review(in: Text, focus?) -> Review:
  context: default
  instruct: concise
  user: Review {{in}}.

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
    assert agic.messages[0].content == "Review {{in}}."
    assert program.flows[0].name == "main"
    assert program.flows[0].stmts[0].kind == "run"


def test_parameters_distinguish_implicit_empty_and_explicit_input() -> None:
    program = Program.from_source(
        """
agic implicit:
  Hello.

agic empty():
  Hello.

agic args(name: Text, detail?):
  Hello.

agic custom(in: Json, detail: Text):
  Hello.
"""
    )

    implicit, empty, args, custom = program.agics
    assert implicit.input is not None and implicit.input.type_name == "Pack"
    assert not implicit.params_explicit
    assert empty.input is None and empty.params == () and empty.params_explicit
    assert args.input is None
    assert [(item.name, item.type_name, item.optional) for item in args.params] == [
        ("name", "Text", False),
        ("detail", None, True),
    ]
    assert custom.input is not None and custom.input.type_name == "Json"
    assert [(item.name, item.type_name) for item in custom.params] == [
        ("detail", "Text")
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


def test_validation_runs_after_lowering() -> None:
    with pytest.raises(ToolangError, match="missing description"):
        Program.from_source(
            """
service github:
  protocol = http
  target = https://mcp.github.com/mcp
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


def test_program_source_strips_header_and_adds_runtime_default_agic(
    tmp_path: Path,
) -> None:
    root = tmp_path / "toolang"
    agent_dir = root / "agents" / "alice"
    agent_dir.mkdir(parents=True)
    (agent_dir / "agent.too").write_text(
        "#!/usr/bin/env toolang\n\nagent alice\n",
        encoding="utf-8",
    )

    prepared = scan_durable_state(root, "alice").load_program()
    program = prepared.parse()

    assert prepared.body_text == ""
    assert program.available_agics[0].name == "default"
    assert program.available_agics[0].input is not None
    assert program.available_agics[0].input.type_name == "Pack"


def test_program_source_preserves_explicit_default_agic(tmp_path: Path) -> None:
    root = _write_program(
        tmp_path,
        """
agic:
  Reply directly.
""".strip(),
    )

    program = scan_durable_state(root, "alice").load_program().parse()

    assert len(program.available_agics) == 1
    assert program.available_agics[0].name == "default"
    assert program.available_agics[0].messages[0].content == "Reply directly."


def test_program_expands_prompt_calls(tmp_path: Path) -> None:
    root = _write_program(
        tmp_path,
        """
prompt review:
  params = path, focus?

  Review {{path}} carefully.
  {{focus}}

agic:
  Respond directly.
""".strip(),
    )
    program = scan_durable_state(root, "alice").load_program().parse()

    expanded = expand_program_input(
        program, '/review src/app.py "only errors"\n\nAlso inspect tests.'
    )

    assert expanded == (
        "Review src/app.py carefully.\nonly errors\n\nAlso inspect tests."
    )


def test_repo_program_fixtures_parse_cleanly() -> None:
    for source_path in sorted(FIXTURES_ROOT.glob("*.too")):
        program = Program.from_source(source_path.read_text(encoding="utf-8"))
        assert program.agics, source_path.name


def test_example_programs_parse_cleanly() -> None:
    for source_path in sorted((PROJECT_ROOT / "examples").glob("*.too")):
        if source_path.name == "invoke-playground.too":
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
