from __future__ import annotations

import pytest

from toolang.catalog import templates


@pytest.mark.parametrize(
    ("kind", "expected_description"),
    [
        ("agent", None),
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
