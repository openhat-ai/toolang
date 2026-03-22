from pathlib import Path

import pytest

from toolang.errors import ToolangError
from toolang.runtime.build import expand_prompt_input, infer_model
from toolang.program import Program, parse

FIXTURE = Path(__file__).parent / "fixtures" / "sample.too"


def test_parse_program_builds_ast() -> None:
    program = parse(FIXTURE.read_text(encoding="utf-8"))

    assert len(program.uses) == 1
    assert len(program.declarations) == 2
    assert len(program.thunks) == 1
    assert program.thunks[0].name == "summarize"
    assert program.thunks[0].output == "WorkspaceSummary"


def test_infer_model_prefers_directive_value() -> None:
    program = parse(FIXTURE.read_text(encoding="utf-8"))

    assert infer_model(program.thunks[0]) == "gpt-5"


def test_expand_prompt_input_renders_template() -> None:
    source = """
prompt summarize(style, audience?): ```md
Summarize the request in a {{style}} style.
Audience: {{audience}}

{{input}}
```

thunk summarize(user):
    Render the summarize prompt.
""".strip()
    program = parse(source)

    expanded = expand_prompt_input(
        program,
        '/summarize style=brief audience="engineering"\n\nShip the parser refactor.',
    )

    assert "brief" in expanded
    assert "engineering" in expanded
    assert "Ship the parser refactor." in expanded


def test_parse_program_rejects_unindented_thunk_body() -> None:
    source = """
thunk summarize:
model = gpt-5
""".strip()

    with pytest.raises(ToolangError, match="Thunk body must be indented"):
        parse(source)


def test_parse_program_keeps_directive_like_lines_after_prompt_in_prompt_body() -> None:
    source = """
thunk summarize:
    First line
    model = gpt-5
""".strip()

    program = parse(source)

    assert program.thunks[0].directives == []
    assert "model = gpt-5" in program.thunks[0].prompt


def test_program_add_cap_ref_and_save_preserves_existing_body(tmp_path: Path) -> None:
    path = tmp_path / "alice.too"
    path.write_text(
        """
thunk review:
    Review the change set.
""".strip(),
        encoding="utf-8",
    )

    program = Program.load(path)

    assert program.add_cap_ref("skill", "by3gus/pdf-processing") is True

    program.save(path)

    assert path.read_text(encoding="utf-8").startswith(
        "use skill by3gus/pdf-processing\n\nthunk review:\n"
    )


def test_program_remove_cap_ref_updates_source_text(tmp_path: Path) -> None:
    path = tmp_path / "alice.too"
    path.write_text(
        """
use skill by3gus/pdf-processing

thunk review:
    Review the change set.
""".strip()
        + "\n",
        encoding="utf-8",
    )

    program = Program.load(path)

    assert program.remove_cap_ref("skill", "pdf-processing") is True

    program.save(path)

    assert path.read_text(encoding="utf-8") == "thunk review:\n    Review the change set.\n"
