from pathlib import Path
from typing import cast

import pytest

from toolang.base.error import ToolangError
from toolang.lang.lower import parse, program_to_ast_data
from toolang.state.durable import scan_durable_state
from toolang.state.program import build_prepared_program, load_live_program


def test_program_parse_projects_embedded_caps_and_structs_into_ast() -> None:
    program = parse(
        """
service github:
  description = Use when the agent needs GitHub MCP access.
  protocol = http
  target = https://mcp.github.com/mcp
  headers = Authorization: Bearer $GITHUB_TOKEN

  Use this service when the agent needs GitHub access.

prompt review:
  params = path, focus?

  Review {{path}} carefully.
  {{focus}}

psyche reviewer:
  Prefer concrete findings and direct language.

struct ReviewSummary:
  title: Text
  summary: Text
""".strip()
    )

    assert [item.kind for item in program.caps] == ["service", "prompt", "psyche"]
    service_cap, prompt_cap, psyche_cap = program.caps
    assert service_cap.meta == {
        "description": "Use when the agent needs GitHub MCP access.",
        "protocol": "http",
        "target": "https://mcp.github.com/mcp",
        "headers": "Authorization: Bearer $GITHUB_TOKEN",
    }
    assert service_cap.body == "Use this service when the agent needs GitHub access."
    assert prompt_cap.meta == {"params": "path, focus?"}
    assert [(item.name, item.optional) for item in prompt_cap.params] == [
        ("path", False),
        ("focus", True),
    ]
    assert prompt_cap.body == "Review {{path}} carefully.\n{{focus}}"
    assert psyche_cap.meta == {}
    assert psyche_cap.body == "Prefer concrete findings and direct language."
    assert len(program.structs) == 1
    assert program.structs[0].name == "ReviewSummary"
    assert [(item.name, item.type_name) for item in program.structs[0].fields] == [
        ("title", "Text"),
        ("summary", "Text"),
    ]


def test_program_ast_marks_explicit_and_implicit_flow_do_statements() -> None:
    program = parse(
        """
flow review:
  summarize
  do refine
""".strip()
    )

    ast = program_to_ast_data(program)
    flow = cast(list[dict[str, object]], ast["items"])[0]

    assert flow["node"] == "flow"
    assert "params" not in flow
    assert flow["statements"] == [
        {
            "node": "do",
            "span": {"line": 2},
            "implicit": True,
            "proc": {
                "kind": "inline",
                "inline": {
                    "node": "thunk",
                    "directives": [],
                    "messages": [{"content": "summarize"}],
                },
            },
        },
        {
            "node": "do",
            "span": {"line": 3},
            "implicit": False,
            "proc": {"kind": "ref", "ref": "refine"},
        },
    ]


def test_program_params_distinguish_default_empty_named_and_custom_input() -> None:
    program = parse(
        """
thunk implicit:
  hi
thunk empty():
  hi
thunk args(arg1: Text, arg2: Json):
  hi
thunk custom(in: Text, arg1: Json):
  hi
flow implicit_flow:
  do step
flow empty_flow():
  do step
flow args_flow(arg1: Text):
  do step
flow custom_flow(in: Pack, arg1: Text):
  do step
""".strip()
    )

    implicit, empty, args, custom = program.thunks
    assert implicit.input is not None
    assert (implicit.input.name, implicit.input.type_name) == ("in", "Pack")
    assert empty.input is None
    assert empty.params == []
    assert args.input is None
    assert [(item.name, item.type_name) for item in args.params] == [("arg1", "Text"), ("arg2", "Json")]
    assert custom.input is not None
    assert (custom.input.name, custom.input.type_name) == ("in", "Text")
    assert [(item.name, item.type_name) for item in custom.params] == [("arg1", "Json")]

    implicit_flow, empty_flow, args_flow, custom_flow = program.flows
    assert implicit_flow.input is not None
    assert (implicit_flow.input.name, implicit_flow.input.type_name) == ("in", "Pack")
    assert empty_flow.input is None
    assert empty_flow.params == []
    assert args_flow.input is None
    assert [(item.name, item.type_name) for item in args_flow.params] == [("arg1", "Text")]
    assert custom_flow.input is not None
    assert (custom_flow.input.name, custom_flow.input.type_name) == ("in", "Pack")
    assert [(item.name, item.type_name) for item in custom_flow.params] == [("arg1", "Text")]

    ast_items = cast(list[dict[str, object]], program_to_ast_data(program)["items"])
    params_by_name = {str(item["name"]): item.get("params") for item in ast_items}
    assert params_by_name["implicit"] is None
    assert params_by_name["empty"] == []
    assert params_by_name["args"] == [
        {"name": "arg1", "optional": False, "type": {"kind": "builtin", "name": "Text", "array_depth": 0}},
        {"name": "arg2", "optional": False, "type": {"kind": "builtin", "name": "Json", "array_depth": 0}},
    ]
    assert params_by_name["custom"] == [
        {"name": "in", "optional": False, "type": {"kind": "builtin", "name": "Text", "array_depth": 0}},
        {"name": "arg1", "optional": False, "type": {"kind": "builtin", "name": "Json", "array_depth": 0}},
    ]
    assert params_by_name["implicit_flow"] is None
    assert params_by_name["empty_flow"] == []
    assert params_by_name["args_flow"] == [
        {"name": "arg1", "optional": False, "type": {"kind": "builtin", "name": "Text", "array_depth": 0}}
    ]
    assert params_by_name["custom_flow"] == [
        {"name": "in", "optional": False, "type": {"kind": "builtin", "name": "Pack", "array_depth": 0}},
        {"name": "arg1", "optional": False, "type": {"kind": "builtin", "name": "Text", "array_depth": 0}},
    ]


def test_program_parse_projects_instructs_into_ast() -> None:
    program = parse(
        """
instruct:
  You are {{runtime.agent.name}}.

instruct strict_json:
  Return only JSON.

thunk review:
  instruct strict_json

  Review the target carefully.
""".strip()
    )

    assert [(item.name, item.body) for item in program.instructs] == [
        (None, "You are {{runtime.agent.name}}."),
        ("strict_json", "Return only JSON."),
    ]
    instruct = program.thunks[0].instruct
    assert instruct is not None
    assert (instruct.kind, instruct.text) == (
        "instruct",
        "strict_json",
    )
    assert [(item.kind, item.text, item.explicit) for item in program.thunks[0].messages] == [
        ("user", "Review the target carefully.", False),
    ]


def test_program_parse_projects_new_context_recall_and_message_block_syntax_into_ast() -> None:
    program = parse(
        """
context:
  Default context for {{runtime.agent.name}}.

context report:
  Include report-specific run context.

instruct strict_json:
  Return strict JSON.

thunk review(in: Part[], path: Path, focus?: Text, labels: Text[]) -> Json:
  models = gpt-5
  recall = history, memory

  context report

  instruct strict_json

  user:
    Review {{path}} with {{focus}}.

  assistant: Ready to review.

  tool:
    {"status":"cached"}

thunk isolated:
  recall = none
  context none
  instruct none
  user: hello
""".strip()
    )

    assert [(item.name, item.body) for item in program.contexts] == [
        (None, "Default context for {{runtime.agent.name}}."),
        ("report", "Include report-specific run context."),
    ]

    review = program.thunks[0]
    assert review.name == "review"
    assert review.input is not None
    assert (review.input.name, review.input.type_name, review.input.optional) == (
        "in",
        "Part[]",
        False,
    )
    assert [(item.name, item.type_name, item.optional) for item in review.params] == [
        ('path', 'Path', False),
        ('focus', 'Text', True),
        ('labels', 'Text[]', False),
    ]
    assert review.output == "Json"
    assert [(item.name, item.operator, item.values) for item in review.directives] == [
        ('models', '=', ("gpt-5",)),
        ('recall', '=', ("history", "memory")),
    ]
    assert review.context is not None
    assert (review.context.kind, review.context.text, review.context.explicit) == (
        "context",
        "report",
        True,
    )
    assert review.instruct is not None
    assert (review.instruct.kind, review.instruct.text, review.instruct.explicit) == (
        "instruct",
        "strict_json",
        True,
    )
    assert [(item.kind, item.text, item.explicit) for item in review.messages] == [
        ("user", "Review {{path}} with {{focus}}.", True),
        ("assistant", "Ready to review.", True),
        ("tool", '{"status":"cached"}', True),
    ]

    isolated = program.thunks[1]
    assert [(item.name, item.operator, item.values) for item in isolated.directives] == [
        ('recall', '=', ("none",)),
    ]
    assert isolated.context is not None
    assert isolated.context.text == "none"
    assert isolated.instruct is not None
    assert isolated.instruct.text == "none"
    assert [(item.kind, item.text) for item in isolated.messages] == [("user", "hello")]


def test_program_parse_accepts_compact_service_env_names() -> None:
    program = parse(
        """
service linear:
  description = Trigger this service when the agent needs Linear MCP access.
  protocol = stdio
  target = uvx mcp-remote https://mcp.linear.app/sse
  env = LINEAR_API_KEY, API_KEY

  Use this service when the agent needs Linear access.
""".strip()
    )

    service_cap = program.caps[0]
    assert service_cap.meta["env"] == "LINEAR_API_KEY, API_KEY"


def test_program_parse_rejects_invalid_service_env_names() -> None:
    with pytest.raises(ToolangError, match="must list environment variable names"):
        parse(
            """
service linear:
  description = Trigger this service when the agent needs Linear MCP access.
  protocol = stdio
  target = uvx mcp-remote https://mcp.linear.app/sse
  env = LINEAR_API_KEY: $LINEAR_API_KEY
""".strip()
        )


def test_program_parse_projects_typed_thunk_params_into_ast() -> None:
    program = parse(
        """
thunk review(in: Part[], path: Path, focus?) -> ReviewSummary:
  models = gpt-5
  skills += review, patch

  Review the target carefully.
""".strip()
    )

    thunk = program.thunks[0]
    assert thunk.name == "review"
    assert thunk.input is not None
    assert (thunk.input.name, thunk.input.type_name, thunk.input.optional) == ("in", "Part[]", False)
    assert [(item.name, item.type_name, item.optional) for item in thunk.params] == [
        ('path', 'Path', False),
        ("focus", None, True),
    ]
    assert thunk.output == "ReviewSummary"
    assert [(item.name, item.operator, item.values) for item in thunk.directives] == [
        ('models', '=', ("gpt-5",)),
        ('skills', '+=', ("review", "patch")),
    ]
    assert [(item.kind, item.text, item.explicit) for item in thunk.messages] == [
        ("user", "Review the target carefully.", False),
    ]


def test_build_prepared_program_rejects_missing_service_description(tmp_path: Path) -> None:
    root = _write_program(
        tmp_path,
        """
service github:
  protocol = http
  target = https://mcp.github.com/mcp

  Use this service when the agent needs GitHub access.
""".strip(),
    )

    durable = scan_durable_state(root, "alice")
    with pytest.raises(ToolangError, match="is missing description"):
        build_prepared_program(durable)


def test_build_prepared_program_accepts_thunk_without_message_blocks(tmp_path: Path) -> None:
    root = _write_program(
        tmp_path,
        "thunk review():\n  models = gpt-5\n\n",
    )

    durable = scan_durable_state(root, "alice")
    prepared = build_prepared_program(durable)
    thunk = load_live_program(prepared).thunks[0]

    assert thunk.name == "review"
    assert thunk.messages == ()


def test_build_prepared_program_rejects_duplicate_default_instruct(tmp_path: Path) -> None:
    root = _write_program(
        tmp_path,
        """
instruct:
  First.

instruct:
  Second.

thunk:
  Reply directly.
""".strip(),
    )

    durable = scan_durable_state(root, "alice")
    with pytest.raises(ToolangError, match="Duplicate instruct name 'default'"):
        build_prepared_program(durable)


def test_build_prepared_program_rejects_reserved_instruct_name(tmp_path: Path) -> None:
    root = _write_program(
        tmp_path,
        """
instruct none:
  Reserved.

thunk:
  Reply directly.
""".strip(),
    )

    durable = scan_durable_state(root, "alice")
    with pytest.raises(ToolangError, match="reserved"):
        build_prepared_program(durable)


def test_build_prepared_program_canonicalizes_implicit_message_thunk(tmp_path: Path) -> None:
    root = _write_program(
        tmp_path,
        """
thunk:
  Reply directly.
""".strip(),
    )

    durable = scan_durable_state(root, "alice")
    prepared = build_prepared_program(durable)
    live = load_live_program(prepared)

    thunk = live.thunks[0]
    assert (thunk.name or "main") == "main"
    assert thunk.input is not None
    assert thunk.params == []
    assert thunk.messages[0].kind == "user"
    assert thunk.messages[0].text == "Reply directly."
    assert thunk.output is None


def test_build_prepared_program_respects_explicit_empty_param_list(tmp_path: Path) -> None:
    root = _write_program(
        tmp_path,
        """
thunk summarize():
  Summarize the current workspace in a concise style.
""".strip(),
    )

    durable = scan_durable_state(root, "alice")
    prepared = build_prepared_program(durable)
    live = load_live_program(prepared)

    thunk = live.thunks[0]
    assert thunk.name == "summarize"
    assert thunk.input is None
    assert thunk.params == []


def test_build_prepared_program_extracts_named_params_and_return_type(tmp_path: Path) -> None:
    root = _write_program(
        tmp_path,
        """
thunk review(in: Part[], path: Path, focus?) -> ReviewResult:
  models = gpt-5

  Review the target carefully.
""".strip(),
    )

    durable = scan_durable_state(root, "alice")
    prepared = build_prepared_program(durable)
    thunk = load_live_program(prepared).thunks[0]

    assert thunk.name == "review"
    assert thunk.input is not None
    assert [(item.name, item.type_name, item.optional) for item in thunk.params] == [
        ('path', 'Path', False),
        ("focus", None, True),
    ]
    assert thunk.output == "ReviewResult"
    assert [(item.name, item.operator, item.values) for item in thunk.directives] == [
        ('models', '=', ("gpt-5",)),
    ]


def test_build_prepared_program_treats_models_directive_as_ordered_csv(tmp_path: Path) -> None:
    root = _write_program(
        tmp_path,
        """
thunk review():
  models = gpt-5, o3

  Review the target carefully.
""".strip(),
    )

    durable = scan_durable_state(root, "alice")
    prepared = build_prepared_program(durable)
    thunk = load_live_program(prepared).thunks[0]

    assert [(item.name, item.operator, item.values) for item in thunk.directives] == [
        ('models', '=', ("gpt-5", "o3")),
    ]


def test_program_parse_preserves_wildcard_selector_directives() -> None:
    program = parse(
        """
thunk review():
  models = deepseek/*
  tools = shell/*, filesystem/read

  Review the target carefully.
""".strip(),
    )

    thunk = program.thunks[0]
    assert [(item.name, item.operator, item.values) for item in thunk.directives] == [
        ('models', '=', ("deepseek/*",)),
        ('tools', '=', ("shell/*", "filesystem/read")),
    ]


def test_program_parse_projects_thunk_routing_directives_into_ast() -> None:
    program = parse(
        """
thunk plan():
  hands += research, summarize
  handoffs = execute

  Plan the work.
""".strip()
    )

    thunk = program.thunks[0]
    assert [(item.name, item.operator, item.values) for item in thunk.directives] == [
        ('hands', '+=', ("research", "summarize")),
        ('handoffs', '=', ("execute",)),
    ]


def test_program_parse_treats_legacy_delegates_as_message_text() -> None:
    program = parse(
        """
thunk plan():
  delegates += research

  Plan the work.
""".strip()
    )

    assert program.thunks[0].messages[0].text == "delegates += research\n\nPlan the work."


def test_program_parse_projects_template_and_message_blocks_into_ast() -> None:
    program = parse(
        """
thunk rewrite(in: Part[], tone?: Text):
  models = gpt-5
  recall = history, memory

  instruct:
    Rewrite the input for the requested tone.

  context default

  user:
    Rewrite the message faithfully.

  assistant: Draft ready.

  tool: Rewrite result.
""".strip()
    )

    thunk = program.thunks[0]
    assert [(item.name, item.operator, item.values) for item in thunk.directives] == [
        ('models', '=', ("gpt-5",)),
        ('recall', '=', ("history", "memory")),
    ]
    instruct = thunk.instruct
    assert instruct is not None
    assert (instruct.kind, instruct.text, instruct.explicit) == (
        "instruct",
        "Rewrite the input for the requested tone.",
        True,
    )
    context = thunk.context
    assert context is not None
    assert (context.kind, context.text, context.explicit) == (
        "context",
        "default",
        True,
    )
    assert [(item.kind, item.text, item.explicit) for item in thunk.messages] == [
        ("user", "Rewrite the message faithfully.", True),
        ("assistant", "Draft ready.", True),
        ("tool", "Rewrite result.", True),
    ]


def test_program_parse_treats_thunk_local_system_as_message_text() -> None:
    program = parse(
        """
thunk rewrite:
  system:
    Removed syntax.
""".strip()
    )

    assert program.thunks[0].messages[0].text == "system:\n  Removed syntax."


def test_build_prepared_program_rejects_multiple_model_directives(tmp_path: Path) -> None:
    root = _write_program(
        tmp_path,
        """
thunk review():
  models = gpt-5
  models = o3

  Review the target carefully.
""".strip(),
    )

    durable = scan_durable_state(root, "alice")
    with pytest.raises(ToolangError, match="at most one models directive"):
        build_prepared_program(durable)


def test_build_prepared_program_rejects_reserved_runtime_parameter_name(tmp_path: Path) -> None:
    root = _write_program(
        tmp_path,
        """
thunk review(runtime):
  Review the target carefully.
""".strip(),
    )

    durable = scan_durable_state(root, "alice")
    with pytest.raises(ToolangError, match="reserved parameter name 'runtime'"):
        build_prepared_program(durable)


def test_build_prepared_program_rejects_routed_model_selectors(tmp_path: Path) -> None:
    root = _write_program(
        tmp_path,
        """
thunk review():
  models = openai/gpt-5@openrouter

  Review the target carefully.
""".strip(),
    )

    durable = scan_durable_state(root, "alice")
    with pytest.raises(ToolangError, match="route-neutral model refs"):
        build_prepared_program(durable)


def test_build_prepared_program_rejects_duplicate_default_thunk_name(tmp_path: Path) -> None:
    root = _write_program(
        tmp_path,
        """
thunk:
  Reply directly.

thunk main:
  Reply again.
""".strip(),
    )

    durable = scan_durable_state(root, "alice")
    with pytest.raises(ToolangError, match="Duplicate thunk name 'main'"):
        build_prepared_program(durable)


def test_build_prepared_program_strips_shebang_before_agent_header(tmp_path: Path) -> None:
    root = tmp_path / "toolang"
    agent_dir = root / "agents" / "alice"
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "agent.too").write_text(
        "#!/usr/bin/env toolang\n\nagent alice\n\nthunk:\n  Reply directly.\n",
        encoding="utf-8",
    )

    durable = scan_durable_state(root, "alice")
    prepared = build_prepared_program(durable)

    assert prepared.body_text == "thunk:\n  Reply directly."
    snapshot = prepared.to_snapshot()
    thunks = cast(list[dict[str, object]], snapshot["thunks"])
    assert thunks[0]["name"] == "main"


def test_live_program_expands_prompt_calls_with_positional_args_and_extra_body(tmp_path: Path) -> None:
    root = _write_program(
        tmp_path,
        """
prompt review:
  params = path, focus?

  Review {{path}} carefully.
  {{focus}}

thunk:
  Respond directly.
""".strip(),
    )

    durable = scan_durable_state(root, "alice")
    prepared = build_prepared_program(durable)
    live = load_live_program(prepared)

    expanded = live.expand_input(
        '/review src/app.py "only errors"\n\nAlso pay attention to tests.'
    )

    assert expanded == (
        "Review src/app.py carefully.\n"
        "only errors\n\n"
        "Also pay attention to tests."
    )


def test_repo_program_fixtures_parse_cleanly() -> None:
    fixtures_dir = Path(__file__).with_name("fixtures")
    for fixture_name in ("sample.too", "source_only.too"):
        program = parse((fixtures_dir / fixture_name).read_text(encoding="utf-8"))
        assert program.thunks, fixture_name


def test_example_programs_parse_cleanly() -> None:
    examples_dir = Path(__file__).resolve().parents[1] / "examples"
    for source_path in sorted(examples_dir.glob("*.too")):
        program = parse(source_path.read_text(encoding="utf-8"))
        assert program.thunks, source_path.name


def _write_program(tmp_path: Path, body_text: str) -> Path:
    root = tmp_path / "toolang"
    agent_dir = root / "agents" / "alice"
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "agent.too").write_text(
        f"agent alice\n\n{body_text}\n",
        encoding="utf-8",
    )
    return root
