from __future__ import annotations

from pathlib import Path
import re
from xml.etree import ElementTree

import pytest

from toolang.execution.assembly import prompts
from toolang.execution.executor.compact import compact_state
from toolang.execution.recall import recall_revisions
from toolang.execution.types import MessageTemplate
from toolang.lang.input import coerce_output


@pytest.mark.parametrize(
    "name",
    [
        "protocol.md",
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

    assert prompt.startswith("<toolang:protocol>")


def test_bundled_protocol_requires_guidance_recall_before_use() -> None:
    prompt = " ".join(prompts.load("protocol.md").split())

    assert "Before using an authorized skill or service" in prompt
    assert "read its current visible skill-guidance or service-guidance" in prompt
    assert "Psyches are resident; prompts are expanded by the runtime" in prompt
    prohibitions = prompt.split("## Don't", 1)[1]
    assert (
        "Treat triggers, pick receipts, memory, or summaries as loaded guidance"
        in prohibitions
    )
    assert "guidance actually present in the model call" in prompt
    assert "call _toolang__pick" in prompt
    assert "matching kind and exact ref" in prompt
    assert "then wait for the guidance user message" in prompt
    assert "changed or withdrawn capability invalidates its old guidance" in prompt
    assert "use a capability after its trigger is withdrawn" in prohibitions
    assert "Service connections, authentication, and tool permissions" in prompt


def test_protocol_uses_markdown_inside_one_runtime_wrapper() -> None:
    prompt = prompts.load("protocol.md")
    framing = re.sub(r"```xml\n.*?\n```", "", prompt, flags=re.S)
    root = ElementTree.fromstring(
        '<root xmlns:toolang="urn:test">' + framing + "</root>"
    )

    assert len(root) == 1
    protocol = root[0]
    assert protocol.tag == "{urn:test}protocol"
    assert len(protocol) == 0
    assert protocol.text is not None
    assert protocol.text.lstrip().startswith("# Toolang\n")
    assert [line for line in protocol.text.splitlines() if line.startswith("# ")] == [
        "# Toolang",
        "# Your role",
        "# Runtime contract",
        "# Follow these rules",
        "# Write Toolang programs",
    ]
    assert [line for line in protocol.text.splitlines() if line.startswith("## ")] == [
        "## Do",
        "## Don't",
    ]


def test_protocol_groups_complete_tag_examples_without_granting_recall() -> None:
    examples = re.findall(r"```xml\n(.*?)\n```", prompts.load("protocol.md"), re.S)
    assert len(examples) == 1
    root = ElementTree.fromstring(
        '<root xmlns:toolang="urn:test">' + examples[0] + "</root>"
    )
    assert [node.tag.removeprefix("{urn:test}") for node in root] == [
        "workspace-access",
        "workspace-rules",
        "skill-trigger",
        "skill-guidance",
        "skill-trigger",
        "hands",
        "handoffs",
        "context",
    ]
    assert root[0].attrib == {"ref": "example-project"}
    assert root[2].attrib == root[3].attrib
    assert root[4].attrib == {"ref": "skill/example-testing", "removed": "true"}
    assert root[5].attrib == {"enabled": "true"}
    assert root[6].attrib == {"enabled": "false"}
    assert recall_revisions((MessageTemplate("user", (examples[0],)),)) == {}


def test_protocol_introduces_toolang_and_addresses_the_agent() -> None:
    prompt = " ".join(prompts.load("protocol.md").split())

    assert "**Toolang** is a language and runtime for agents and humans." in prompt
    assert "Toolang lets users express know-how" in prompt
    assert "Toolang programs are written in .too files." in prompt
    assert "**Agic** is the basic unit of agentic programs" in prompt
    assert "**Flow** organizes agics and other flows" in prompt
    assert "**Caps** are composable agent primitives" in prompt
    assert "You interpret requests, reason, respond, and choose tool calls" in prompt
    assert (
        "you receive instructions, messages, tool definitions, and an output schema"
        in prompt
    )
    assert "Interpret toolang:TAG XML tags in your instructions and messages" in prompt
    assert "syntax is not YAML" not in prompt


def test_protocol_distinguishes_runtime_and_workspace_boundaries() -> None:
    prompt = " ".join(prompts.load("protocol.md").split())

    assert "Setup stays fixed within a root run" in prompt
    assert "output contract stays fixed within one agic invocation" in prompt
    assert 'removed="true" withdraws the resource' in prompt
    assert "initially in instructions and later in messages when changed" in prompt
    assert "Before fs operations, the runtime loads applicable rules" in prompt
    assert "Shell preflight checks cwd, not paths inside commands" in prompt
    assert "actively load their applicable AGENTS.md files" in prompt


def test_protocol_separates_contract_required_actions_and_prohibitions() -> None:
    prompt = prompts.load("protocol.md")
    contract, rules = prompt.split("# Runtime contract\n", 1)[1].split(
        "# Follow these rules\n", 1
    )
    rules = rules.split("# Write Toolang programs\n", 1)[0]
    assert rules.startswith("\n## Do\n")
    actions, prohibitions = rules.split("## Do\n", 1)[1].split("## Don't\n", 1)

    assert "Follow protocol, then instruct, then selected psyches" in contract
    assert 'removed="true"' in contract
    assert "Use reload" not in contract
    assert re.findall(r"^(\d+)\. \*\*", actions, re.M) == [str(i) for i in range(1, 8)]
    assert len(re.findall(r"^- ", prohibitions, re.M)) == 5


def test_protocol_authoring_requires_verification_and_state_adoption() -> None:
    authoring = " ".join(
        prompts.load("protocol.md").split("# Write Toolang programs\n", 1)[1].split()
    )

    assert "first load the relevant grammar, coding-convention, and CLI guidance" in (
        authoring
    )
    assert "If unavailable, consult" in authoring
    assert "documentation matching that runtime" in authoring
    assert "actual launcher's --version and --help" in authoring
    assert "development may use uv run toolang" in authoring
    assert (
        "If you cannot verify syntax or a command, state the uncertainty" in authoring
    )
    assert "ask for the missing information. Do not guess" in authoring
    assert "Use permitted me tools for home caps and flows" in authoring
    assert (
        "for agent.too or Setup, provide source or obtain an authorized editing path"
        in (authoring)
    )
    assert "Validate with that runtime" in authoring
    assert "Do not edit immutable State or execution records" in authoring
    assert "treat a source write as adopted State" in authoring
    assert "Use reload when the current run needs newly authored State" in authoring
    assert "load current guidance before using changed skills or services" in (
        authoring
    )
    assert "or assume it grants permissions" in authoring


def test_protocol_authoring_references_exist() -> None:
    authoring = prompts.load("protocol.md").split("# Write Toolang programs\n", 1)[1]
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


def test_protocol_authoring_is_conditional_and_points_to_details() -> None:
    authoring = prompts.load("protocol.md").split("# Write Toolang programs\n", 1)[1]
    assert authoring.lstrip().startswith(
        "When asked to write or modify a Toolang program,"
    )
    opening = authoring.strip().split("\n\n", 1)[0]
    assert "[toolang-syntax]" in opening
    assert "[caps files]" in opening and "[coding conventions]" in opening
    assert "[program syntax]" not in authoring
    assert "flow-syntax.md" not in authoring
    assert "```" not in authoring


@pytest.mark.parametrize("field", ["thread", "begin", "end", "previous_summary"])
def test_compact_requires_concrete_inputs_and_returns_only_text(field: str) -> None:
    from toolang.lang.input import resolve_runnable_input

    program = compact_state().modules["agent"]
    assert not program.flows and not program.structs
    assert len(program.agics) == 1
    agic = program.find_agic("compact")
    assert agic is not None and agic.output == "Text"
    assert agic.instruct is None
    assert all(message.role == "user" for message in agic.messages)
    assert coerce_output("Notes.", agic.output) == "Notes."
    values = {
        "thread": "term_a",
        "begin": "run_a",
        "end": "run_b",
        "previous_summary": "",
    }
    del values[field]
    with pytest.raises(ValueError, match=field):
        resolve_runnable_input(agic, values)
