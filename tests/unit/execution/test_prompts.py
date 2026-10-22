"""The bundled protocol remains parseable and states the runtime boundaries."""

from pathlib import Path
import re
from xml.etree import ElementTree

import pytest

from toolang.execution.assembly import prompts
from toolang.plugin.toolsets.loading import load_tools
from toolang.state.builtin import talk_handler_source


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


@pytest.mark.parametrize(
    "text",
    [prompts.load("protocol.md"), talk_handler_source()],
    ids=["protocol", "talk"],
)
def test_prompt_tool_calls_reference_registered_model_callable_tools(text: str) -> None:
    names = set(re.findall(r"\b[A-Za-z_][A-Za-z_]*__[A-Za-z][A-Za-z_]*\b", text))
    tools = load_tools()
    assert names
    assert names <= set(tools)
    assert all(getattr(tools[name], "model_callable", True) for name in names)


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
        "Tool names and conversations",
        "Choosing and completing work",
        "Workspaces and paths",
        "Authoring Toolang",
    ]


def test_protocol_preserves_guidance_and_execution_boundaries() -> None:
    prompt = " ".join(prompts.load("protocol.md").split())
    for requirement in (
        "A pick receipt is not guidance",
        "call runtime__pick",
        "wait for the guidance user message",
        "A trigger change or removal invalidates",
        "Setup, which stays fixed within a root Run",
        "output contract stays fixed",
        "Run (including async run) and spawn cannot target the current runnable or an ancestor",
        "child self-exec is forbidden",
        'entered_by="exec"',
        "the handoff has already succeeded",
        "runtime__runnables",
        "Missing routes means ALL",
        "runtime__chdir alone",
        "Shell preflight checks cwd, not paths inside commands",
    ):
        assert requirement in prompt
    assert "requested_only" not in prompt
    assert "me.sync()" not in prompt


@pytest.mark.parametrize(
    "requirements",
    [
        (
            "Final output ends this handler and is not delivered to the conversation",
            "Before ending, check the convo__write receipts",
            "Record why each ignored message needs no action",
        ),
        (
            "Send a brief progress reply before worker discovery or task investigation",
            "Substantial work includes file changes, coding, tests, multi-step research",
            "Do not perform that work in this handler",
        ),
        (
            "Use runtime__runnables to inspect worker targets and their input signatures",
            "Omit thread for new work so runtime__spawn creates a work_ thread",
            "Do not use runtime__run, runtime__exec, runtime__await, or polling to complete the work here",
            "After confirmed admission, send a dispatch notice and end this handler",
            "report its result or blocker via convo__write",
        ),
    ],
    ids=["visible-outcomes", "early-feedback", "independent-dispatch"],
)
def test_talk_handler_requires_visible_outcomes_and_independent_dispatch(
    requirements: tuple[str, ...],
) -> None:
    prompt = " ".join(talk_handler_source().split())
    for requirement in requirements:
        assert requirement in prompt


def test_protocol_authoring_references_exist() -> None:
    authoring = prompts.load("protocol.md").split("# Authoring Toolang\n", 1)[1]
    paths = re.findall(
        r"https://github.com/openhat-ai/toolang/blob/main/(docs/[^)]+)", authoring
    )
    assert paths == [
        "docs/design/program.md",
        "docs/design/caps.md",
        "docs/authoring.md",
    ]
    root = Path(__file__).resolve().parents[3]
    assert all((root / path).is_file() for path in paths)
    assert "Saving files does not publish State" in authoring
