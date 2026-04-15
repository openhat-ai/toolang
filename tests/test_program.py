from pathlib import Path

import pytest

from toolang.base.error import ToolangError
from toolang.program import parse
from toolang.state.durable import scan_durable_state
from toolang.state.program import build_prepared_program


def test_program_parse_projects_prompt_params_into_ast() -> None:
    program = parse(
        """
prompt rewrite(style): ```md
Rewrite this in a {{style}} style.
```
""".strip()
    )

    assert len(program.declarations) == 1
    assert program.declarations[0].kind == "prompt"
    assert [item.name for item in program.declarations[0].params] == ["style"]


def test_build_prepared_program_rejects_reserved_prompt_param_name(tmp_path: Path) -> None:
    root = _write_program(
        tmp_path,
        """
prompt rewrite(input): ```md
Rewrite this:

{{input}}
```
""".strip(),
    )

    durable = scan_durable_state(root, "alice")
    with pytest.raises(ToolangError, match="Prompt parameter name 'input' is reserved"):
        build_prepared_program(durable)


def test_build_prepared_program_rejects_empty_thunk_prompt(tmp_path: Path) -> None:
    program = parse(
        """
thunk review:
    model = gpt-5
""".strip()
    )
    assert program.thunks[0].prompt == ""

    root = _write_program(
        tmp_path,
        """
thunk review:
    model = gpt-5
""".strip(),
    )

    durable = scan_durable_state(root, "alice")
    with pytest.raises(ToolangError, match="Thunk 'review' is missing prompt text"):
        build_prepared_program(durable)


def _write_program(tmp_path: Path, body_text: str) -> Path:
    root = tmp_path / "toolang"
    agent_dir = root / "agents" / "alice"
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "alice.too").write_text(
        f"agent alice\n\n{body_text}\n",
        encoding="utf-8",
    )
    return root
