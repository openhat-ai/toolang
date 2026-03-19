from pathlib import Path

from toolang.parser import parse_program
from toolang.runtime import expand_prompt_input, infer_model

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
