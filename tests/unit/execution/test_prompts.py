"""The bundled protocol remains parseable and states the runtime boundaries."""

from pathlib import Path
import re
from xml.etree import ElementTree

import pytest

from toolang.execution.assembly import prompts


@pytest.mark.parametrize(
    "name", ["protocol.md", "defaults/instruct.md", "defaults/context.md"]
)
def test_bundled_prompt_resources_are_loadable(name: str) -> None:
    assert prompts.load(name).strip()


def test_bundled_prompt_loading_does_not_depend_on_package_metadata(
    monkeypatch,
) -> None:
    prompts.load.cache_clear()
    monkeypatch.setattr(prompts, "__package__", None)
    try:
        prompt = prompts.load("protocol.md")
    finally:
        prompts.load.cache_clear()
    assert prompt.startswith("<toolang:protocol>")


def test_protocol_is_one_literal_body_with_action_focused_sections() -> None:
    root = ElementTree.fromstring(
        '<root xmlns:toolang="urn:test">' + prompts.load("protocol.md") + "</root>"
    )
    assert len(root) == 1 and root[0].tag == "{urn:test}protocol"
    assert len(root[0]) == 0
    sections = re.findall(r"^# (.+)$", root[0].text or "", re.M)
    assert sections == [
        "Role and instruction sources",
        "Runtime facts and resource messages",
        "Choosing and completing work",
        "Workspaces and paths",
        "Authoring Toolang",
    ]


def test_protocol_preserves_guidance_and_execution_boundaries() -> None:
    prompt = " ".join(prompts.load("protocol.md").split())
    for requirement in (
        "A pick receipt is not guidance",
        "call _toolang__pick",
        "wait for the guidance user message",
        "A trigger change or removal invalidates",
        "Setup, which stays fixed within a root Run",
        "output contract stays fixed",
        "Run (including async run) and spawn cannot target the current runnable or an ancestor",
        "child self-exec is forbidden",
        'entered_by="exec"',
        "the handoff has already succeeded",
        "_toolang__runnables",
        "Missing routes means ALL",
        "_toolang__chdir alone",
        "Shell preflight checks cwd, not paths inside commands",
    ):
        assert requirement in prompt
    assert "requested_only" not in prompt
    assert "me.sync()" not in prompt


def test_protocol_authoring_references_exist() -> None:
    authoring = prompts.load("protocol.md").split("# Authoring Toolang\n", 1)[1]
    paths = re.findall(
        r"https://github.com/openhat-ai/toolang/blob/main/(docs/[^)]+)", authoring
    )
    assert paths == [
        "docs/program.md",
        "docs/caps.md",
        "docs/toolang-authoring-conventions.md",
    ]
    root = Path(__file__).resolve().parents[3]
    assert all((root / path).is_file() for path in paths)
    assert "Saving files does not publish State" in authoring
