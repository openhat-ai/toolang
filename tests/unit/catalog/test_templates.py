from __future__ import annotations

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
        ("task", "Task title"),
    ],
)
def test_load_default_template(
    kind: templates.TemplateKind,
    expected_description: str | None,
) -> None:
    template = templates.load_template(kind)

    assert template.kind == kind
    assert template.name == "default"
    assert template.path.startswith(f"{kind}.default.")
    assert template.description == expected_description


def test_list_templates_puts_default_first() -> None:
    assert [template.name for template in templates.list_templates("service")] == [
        "default",
        "stdio",
    ]


def test_render_template_replaces_bindings() -> None:
    rendered = templates.render_template("prompt", input="catalog input")

    assert "Use `catalog input` for the caller input." in rendered


def test_load_template_rejects_unknown_name() -> None:
    with pytest.raises(FileNotFoundError, match="template not found: task.unknown"):
        templates.load_template("task", "unknown")


def test_agent_template_declares_a_minimal_unnamed_entry() -> None:
    from toolang.lang import Program

    program = Program.from_source(templates.load_template("agent").raw_text)

    assert program.flows == ()
    (agic,) = program.agics
    assert agic.name is None
    assert agic.input is not None and agic.input.name == "_"
    assert [message.content for message in agic.messages] == ["{{_}}"]
