from pathlib import Path

import pytest

from toolang.base.error import ToolangError
from toolang.program import parse
from toolang.state.durable import scan_durable_state
from toolang.state.program import build_prepared_program, load_live_program


def test_program_parse_projects_inline_caps_and_structs_into_ast() -> None:
    program = parse(
        """
service github: ```md
---
transport: http
url: https://mcp.github.com/mcp
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
        "transport": "http",
        "url": "https://mcp.github.com/mcp",
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


def test_program_parse_projects_typed_thunk_params_into_ast() -> None:
    program = parse(
        """
thunk review(_, path: path, focus?) -> ReviewSummary:
  model = gpt-5
  skills += review, patch

  Review the target carefully.
""".strip()
    )

    thunk = program.thunks[0]
    assert thunk.name == "review"
    assert thunk.params_omitted is False
    assert [(item.name, item.message, item.type_name, item.optional) for item in thunk.params] == [
        ("_", True, None, False),
        ("path", False, "path", False),
        ("focus", False, None, True),
    ]
    assert thunk.returns == "ReviewSummary"
    assert thunk.directives == ["model = gpt-5", "skills += review, patch"]
    assert thunk.body == "Review the target carefully."


def test_build_prepared_program_rejects_missing_service_frontmatter_in_grammar(tmp_path: Path) -> None:
    root = _write_program(
        tmp_path,
        """
service github: ```md
Use this service when the agent needs GitHub access.
```
""".strip(),
    )

    durable = scan_durable_state(root, "alice")
    with pytest.raises(ToolangError, match="Syntax error at line 1"):
        build_prepared_program(durable)


def test_build_prepared_program_rejects_empty_thunk_body(tmp_path: Path) -> None:
    root = _write_program(
        tmp_path,
        "thunk review():\n  model = gpt-5\n\n",
    )

    durable = scan_durable_state(root, "alice")
    with pytest.raises(ToolangError, match="Thunk 'review' is missing body text"):
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

    assert prepared.thunks[0].name == "main"
    assert prepared.thunks[0].accepts_message is True
    assert prepared.thunks[0].params == ()
    assert prepared.thunks[0].body == "Reply directly."
    assert prepared.thunks[0].returns is None


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

    assert prepared.thunks[0].name == "summarize"
    assert prepared.thunks[0].accepts_message is False
    assert prepared.thunks[0].params == ()


def test_build_prepared_program_extracts_named_params_and_return_type(tmp_path: Path) -> None:
    root = _write_program(
        tmp_path,
        """
thunk review(_, path: path, focus?) -> ReviewResult:
  model = gpt-5

  Review the target carefully.
""".strip(),
    )

    durable = scan_durable_state(root, "alice")
    prepared = build_prepared_program(durable)
    thunk = prepared.thunks[0]

    assert thunk.name == "review"
    assert thunk.accepts_message is True
    assert [(item.name, item.type_name, item.optional) for item in thunk.params] == [
        ("path", "path", False),
        ("focus", None, True),
    ]
    assert thunk.returns == "ReviewResult"
    assert thunk.directives == ("model = gpt-5",)


def test_build_prepared_program_treats_model_directive_as_ordered_csv(tmp_path: Path) -> None:
    root = _write_program(
        tmp_path,
        """
thunk review():
  model = gpt-5, o3

  Review the target carefully.
""".strip(),
    )

    durable = scan_durable_state(root, "alice")
    prepared = build_prepared_program(durable)
    thunk = prepared.thunks[0]

    assert thunk.directives == ("model = gpt-5, o3",)
    assert thunk.model_selectors() == ("gpt-5", "o3")
    assert thunk.model_selector() == "gpt-5"


def test_build_prepared_program_rejects_multiple_model_directives(tmp_path: Path) -> None:
    root = _write_program(
        tmp_path,
        """
thunk review():
  model = gpt-5
  model = o3

  Review the target carefully.
""".strip(),
    )

    durable = scan_durable_state(root, "alice")
    with pytest.raises(ToolangError, match="at most one model directive"):
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
    (agent_dir / "alice.too").write_text(
        "#!/usr/bin/env toolang\n\nagent alice\n\nthunk:\n  Reply directly.\n",
        encoding="utf-8",
    )

    durable = scan_durable_state(root, "alice")
    prepared = build_prepared_program(durable)

    assert prepared.body_text == "thunk:\n  Reply directly."
    assert prepared.thunks[0].name == "main"


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
    (agent_dir / "alice.too").write_text(
        f"agent alice\n\n{body_text}\n",
        encoding="utf-8",
    )
    return root
