from __future__ import annotations

import pytest

from toolang.execution.executor import prompts


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
