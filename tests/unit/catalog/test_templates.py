from __future__ import annotations

from importlib.resources import files

import pytest

from toolang.catalog import templates


@pytest.mark.parametrize(
    ("kind", "expected_description"),
    [
        ("agent", None),
        ("script", None),
        ("chore", "Chore title"),
        ("prompt", None),
        ("psyche", None),
        (
            "service",
            "Trigger this service when the agent needs this remote MCP server.",
        ),
        ("skill", "Trigger this skill for requests that need this workflow."),
    ],
)
def test_load_default_template(
    kind: templates.TemplateKind,
    expected_description: str | None,
) -> None:
    template = templates.load_template(kind)

    assert template.kind == kind
    assert template.path.startswith(f"{kind}.default.")
    assert template.description == expected_description


def test_bundled_templates_contain_only_one_default_per_kind() -> None:
    assert {
        item.name
        for item in files("toolang.catalog.templates").iterdir()
        if item.name.endswith((".md", ".too"))
    } == {
        "agent.default.too",
        "script.default.too",
        "chore.default.md",
        "prompt.default.md",
        "psyche.default.md",
        "service.default.md",
        "skill.default.md",
    }


def test_agent_template_declares_a_minimal_unnamed_entry() -> None:
    from toolang.lang import Program

    assert templates.load_template("agent").raw_text == "agic:\n  {{_}}\n"
    program = Program.from_source(templates.load_template("agent").raw_text)

    assert program.flows == ()
    (agic,) = program.agics
    assert agic.name is None
    assert agic.input is not None and agic.input.name == "_"
    assert [message.content for message in agic.messages] == ["{{_}}"]
