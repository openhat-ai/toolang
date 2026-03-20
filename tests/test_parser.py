from pathlib import Path

import pytest

from toolang.errors import ToolangError
from toolang.runtime.build import expand_prompt_input, infer_model
from toolang.syntax import parse_program

FIXTURE = Path(__file__).parent / "fixtures" / "sample.too"


def test_parse_program_builds_ast() -> None:
    program = parse_program(FIXTURE.read_text(encoding="utf-8"))

    assert len(program.uses) == 1
    assert len(program.declarations) == 2
    assert len(program.thunks) == 1
    assert program.thunks[0].name == "summarize"
    assert program.thunks[0].output == "WorkspaceSummary"


def test_infer_model_prefers_directive_value() -> None:
    program = parse_program(FIXTURE.read_text(encoding="utf-8"))

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
    program = parse_program(source)

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
        parse_program(source)


def test_parse_program_keeps_directive_like_lines_after_prompt_in_prompt_body() -> None:
    source = """
thunk summarize:
    First line
    model = gpt-5
""".strip()

    program = parse_program(source)

    assert program.thunks[0].directives == []
    assert "model = gpt-5" in program.thunks[0].prompt
