from __future__ import annotations

import pytest

from toolang.base.errors import ToolangError
from toolang.execution.executor import prompts
from toolang.execution.executor.compact import compact_state
from toolang.lang.input import coerce_output


def test_bundled_prompt_loading_does_not_depend_on_package_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompts.load.cache_clear()
    monkeypatch.setattr(prompts, "__package__", None)

    try:
        prompt = prompts.load("instruct.default.md")
    finally:
        prompts.load.cache_clear()

    assert prompt.startswith("<runtime-instructions>")


@pytest.mark.parametrize("name", ["compact_start", "compact_page"])
def test_compact_progress_requires_summary_position_and_completion(name: str) -> None:
    program = compact_state().modules["agent"]
    agic = program.find_agic(name)
    assert agic is not None and agic.output is not None
    structs = {s.name: s for s in program.structs}
    progress = {"summary": "", "position": None, "complete": False}
    coerce_output(progress, agic.output, structs=structs)
    with pytest.raises(ToolangError, match="missing CompactProgress fields: complete"):
        coerce_output(
            {"summary": "", "position": {"complete": False}},
            agic.output,
            structs=structs,
        )
