from pathlib import Path
from typing import cast

import pytest

from toolang.base.error import ToolangError
from toolang.program import parse
from toolang.state.durable import scan_durable_state
from toolang.state.program import build_prepared_program, load_live_program


def test_program_parse_projects_embedded_caps_and_structs_into_ast() -> None:
    program = parse(
        """
service github: ```md
---
description: Use when the agent needs GitHub MCP access.
transport: http
target: https://mcp.github.com/mcp
headers:
  Authorization: Bearer $GITHUB_TOKEN
---

Use this service when the agent needs GitHub access.
```

prompt review: ```md
---
params: path, focus?
---

Review {{path}} carefully.
{{focus}}
```

psyche reviewer: ```md
Prefer concrete findings and direct language.
```

struct ReviewSummary:
  title: string
  summary: string
""".strip()
    )

    assert [item.kind for item in program.declarations] == ["service", "prompt", "psyche"]
    service_decl, prompt_decl, psyche_decl = program.declarations
    assert service_decl.meta == {
        "description": "Use when the agent needs GitHub MCP access.",
        "transport": "http",
        "target": "https://mcp.github.com/mcp",
        "headers": {"Authorization": "Bearer $GITHUB_TOKEN"},
    }
    assert service_decl.body == "Use this service when the agent needs GitHub access."
    assert prompt_decl.meta == {"params": "path, focus?"}
    assert [(item.name, item.optional) for item in prompt_decl.params] == [
        ("path", False),
        ("focus", True),
    ]
    assert prompt_decl.body == "Review {{path}} carefully.\n{{focus}}"
    assert psyche_decl.meta == {}
    assert psyche_decl.body == "Prefer concrete findings and direct language."
    assert len(program.structs) == 1
    assert program.structs[0].name == "ReviewSummary"
    assert [(item.name, item.type_name) for item in program.structs[0].fields] == [
        ("title", "string"),
        ("summary", "string"),
    ]


def test_program_parse_projects_instructs_into_ast() -> None:
    program = parse(
        """
instruct: ```md
You are {{runtime.agent.name}}.
```

instruct strict-json:
  Return only JSON.

thunk review:
  instruct: strict-json

  Review the target carefully.
""".strip()
    )

    assert [(item.name, item.body) for item in program.instructs] == [
        (None, "You are {{runtime.agent.name}}."),
        ("strict-json", "Return only JSON."),
    ]
    instruct = program.thunks[0].instruct
    assert instruct is not None
    assert (instruct.kind, instruct.text) == (
        "instruct",
        "strict-json",
    )
    assert [(item.kind, item.text, item.explicit) for item in program.thunks[0].messages] == [
        ("user", "Review the target carefully.", False),
    ]


def test_program_parse_projects_new_context_recall_and_message_block_syntax_into_ast() -> None:
    program = parse(
        """
context: ```md
Default context for {{runtime.agent.name}}.
```

context report:
  Include report-specific run context.

instruct strict-json:
  Return strict JSON.

thunk review(input: Message, path: Path, focus?: Text, labels: Text[]) -> Json:
  models = gpt-5
  recall = history, memory

  context: report

  instruct: strict-json

  user:
    Review {{path}} with {{focus}}.

  assistant: Ready to review.

  tool:
    {"status":"cached"}

thunk isolated:
  recall = none
  context: none
  instruct: none
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
        "_",
        None,
        False,
    )
    assert [(item.name, item.type_name, item.optional) for item in review.params] == [
        ("path", "path", False),
        ("focus", "string", True),
        ("labels", "string[]", False),
    ]
    assert review.output == "json"
    assert [(item.kind, item.op, item.items) for item in review.directives] == [
        ("model", "set", ("gpt-5",)),
        ("recall", "set", ("history", "memory")),
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
        "strict-json",
        True,
    )
    assert [(item.kind, item.text, item.explicit) for item in review.messages] == [
        ("user", "Review {{path}} with {{focus}}.", True),
        ("assistant", "Ready to review.", True),
        ("tool", '{"status":"cached"}', True),
    ]

    isolated = program.thunks[1]
    assert [(item.kind, item.op, item.items) for item in isolated.directives] == [
        ("recall", "set", ("none",)),
    ]
    assert isolated.context is not None
    assert isolated.context.text == "none"
    assert isolated.instruct is not None
    assert isolated.instruct.text == "none"
    assert [(item.kind, item.text) for item in isolated.messages] == [("user", "hello")]


def test_program_parse_accepts_compact_service_env_names() -> None:
    program = parse(
        """
service linear: ```md
---
description: Trigger this service when the agent needs Linear MCP access.
transport: stdio
target: uvx mcp-remote https://mcp.linear.app/sse
env: LINEAR_API_KEY, API_KEY
---

Use this service when the agent needs Linear access.
```
""".strip()
    )

    service_decl = program.declarations[0]
    assert service_decl.meta["env"] == "LINEAR_API_KEY, API_KEY"


def test_program_parse_rejects_service_env_map_syntax() -> None:
    with pytest.raises(ToolangError, match="must list environment variable names"):
        parse(
            """
service linear: ```md
---
description: Trigger this service when the agent needs Linear MCP access.
transport: stdio
target: uvx mcp-remote https://mcp.linear.app/sse
env:
  LINEAR_API_KEY: $LINEAR_API_KEY
---
```
""".strip()
        )


def test_program_parse_projects_typed_thunk_params_into_ast() -> None:
    program = parse(
        """
thunk review(_, path: path, focus?) -> ReviewSummary:
  models = gpt-5
  skills += review, patch

  Review the target carefully.
""".strip()
    )

    thunk = program.thunks[0]
    assert thunk.name == "review"
    assert thunk.input is not None
    assert (thunk.input.name, thunk.input.type_name, thunk.input.optional) == ("_", None, False)
    assert [(item.name, item.type_name, item.optional) for item in thunk.params] == [
        ("path", "path", False),
        ("focus", None, True),
    ]
    assert thunk.output == "ReviewSummary"
    assert [(item.kind, item.op, item.items) for item in thunk.overlays] == [
        ("model", "set", ("gpt-5",)),
        ("skill", "add", ("review", "patch")),
    ]
    assert [(item.kind, item.text, item.explicit) for item in thunk.messages] == [
        ("user", "Review the target carefully.", False),
    ]


def test_build_prepared_program_rejects_missing_service_frontmatter(tmp_path: Path) -> None:
    root = _write_program(
        tmp_path,
        """
service github: ```md
Use this service when the agent needs GitHub access.
```
""".strip(),
    )

    durable = scan_durable_state(root, "alice")
    with pytest.raises(ToolangError, match="is missing frontmatter"):
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
thunk review(_, path: path, focus?) -> ReviewResult:
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
        ("path", "path", False),
        ("focus", None, True),
    ]
    assert thunk.output == "ReviewResult"
    assert [(item.kind, item.op, item.items) for item in thunk.overlays] == [
        ("model", "set", ("gpt-5",)),
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

    assert [(item.kind, item.op, item.items) for item in thunk.overlays] == [
        ("model", "set", ("gpt-5", "o3")),
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
    assert [(item.kind, item.op, item.items) for item in thunk.overlays] == [
        ("model", "set", ("deepseek/*",)),
        ("tool", "set", ("shell/*", "filesystem/read")),
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
    assert [(item.kind, item.op, item.items) for item in thunk.overlays] == [
        ("hand", "add", ("research", "summarize")),
        ("handoff", "set", ("execute",)),
    ]


def test_program_parse_rejects_legacy_delegates_directive() -> None:
    with pytest.raises(ToolangError, match="line 2"):
        parse(
            """
thunk plan():
  delegates += research

  Plan the work.
""".strip()
        )


def test_program_parse_projects_template_and_message_blocks_into_ast() -> None:
    program = parse(
        """
thunk rewrite(_, tone?: string):
  models = gpt-5
  recall = history, memory

  instruct:
    Rewrite the input for the requested tone.

  context: default

  user:
    Rewrite the message faithfully.

  assistant: Draft ready.

  tool: Rewrite result.
""".strip()
    )

    thunk = program.thunks[0]
    assert [(item.kind, item.op, item.items) for item in thunk.overlays] == [
        ("model", "set", ("gpt-5",)),
        ("recall", "set", ("history", "memory")),
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


def test_program_parse_rejects_thunk_local_system_block() -> None:
    with pytest.raises(ToolangError, match="line 2"):
        parse(
            """
thunk rewrite:
  system:
    Removed syntax.
""".strip()
        )


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
prompt review: ```md
---
params: path, focus?
---

Review {{path}} carefully.
{{focus}}
```

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
