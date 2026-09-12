from __future__ import annotations

import pytest

from toolang.base.errors import ToolangError
from toolang.execution.assembly import prompts
from toolang.execution.executor.compact import compact_state
from toolang.lang.input import coerce_output


@pytest.mark.parametrize(
    "name",
    [
        "protocol.md",
        "psyches.md",
        "skills.md",
        "services.md",
        "tools.md",
        "filesystem.md",
        "steer.md",
        "cancel.md",
        "output-repair.md",
        "defaults/instruct.md",
        "defaults/context.md",
        "defaults/compact.too",
    ],
)
def test_bundled_prompt_resources_are_loadable(name: str) -> None:
    assert prompts.load(name).strip()


def test_bundled_prompt_loading_does_not_depend_on_package_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompts.load.cache_clear()
    monkeypatch.setattr(prompts, "__package__", None)

    try:
        prompt = prompts.load("protocol.md")
    finally:
        prompts.load.cache_clear()

    assert prompt.startswith("<runtime-instructions>")


def test_bundled_protocol_requires_guidance_recall_before_use() -> None:
    prompt = prompts.load("protocol.md")

    assert "Catalogs are an index, not loaded guidance" in prompt
    assert "call `_toolang__pick`" in prompt
    assert "only after that message is visible" in prompt
    assert "Picking a service does not connect" in prompt


@pytest.mark.parametrize("field", ["thread", "begin", "end", "summary"])
def test_compact_declares_one_agic_with_structured_coverage(field: str) -> None:
    program = compact_state().modules["agent"]
    assert not program.flows
    assert len(program.agics) == 1
    agic = program.find_agic("compact")
    assert agic is not None and agic.output is not None
    structs = {s.name: s for s in program.structs}
    output = {"thread": "term_a", "begin": None, "end": "run_b", "summary": "Notes."}
    coerce_output(output, agic.output, structs=structs)
    del output[field]
    with pytest.raises(ToolangError, match=f"missing Compacted fields: {field}"):
        coerce_output(output, agic.output, structs=structs)
