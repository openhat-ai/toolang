"""Prompt composition preserves section boundaries and literal guidance."""

from itertools import product

import pytest

from toolang.common.template import render_text_template
from toolang.execution import prompts
from toolang.execution.prompting import (
    render_instructions,
    render_tool_instructions,
)
from toolang.lang import Program


@pytest.mark.parametrize("kind", ["skill", "service"])
@pytest.mark.parametrize("selected", [False, True])
def test_capability_templates_render_only_their_own_catalog(
    kind: str, selected: bool
) -> None:
    other = "service" if kind == "skill" else "skill"
    context = {
        f"has_{kind}s": selected,
        f"{kind}s": [{"name": "selected", "ref": f"home://{kind}s/selected"}],
        f"has_{other}s": True,
        f"{other}s": [{"name": "unrelated"}],
    }

    rendered = render_text_template(prompts.load(f"{kind}s.md"), context).strip()

    assert "<capability-catalog>" not in rendered
    assert f"<{other}s>" not in rendered
    if selected:
        assert rendered.startswith(f"<{kind}s>")
        assert rendered.endswith(f"</{kind}s>")
        assert f'ref="home://{kind}s/selected"' in rendered
    else:
        assert rendered == ""


@pytest.mark.parametrize("skills,services", tuple(product((False, True), repeat=2)))
def test_catalog_composition_preserves_framing_order_and_spacing(
    skills: bool, services: bool
) -> None:
    program = Program.from_source("agic chat:\n  instruct: none\n  Hello.\n")
    context: dict[str, object] = {
        "has_skills": skills,
        "skills": [{"name": "one"}],
        "has_services": services,
        "services": [{"name": "two"}],
    }

    rendered = render_instructions(program, program.agics[0], context)

    if not skills and not services:
        assert "<capability-catalog>" not in rendered
        return
    skill_section = (
        '<skills>\n<available>\n<skill name="one" scope="" origin="" form="" ref="">\n'
        "</skill>\n</available>\n</skills>\n"
        if skills
        else ""
    )
    service_section = (
        '<services>\n<available>\n<service name="two" scope="" origin="" form="" ref="">\n'
        "</service>\n</available>\n</services>\n"
        if services
        else ""
    )
    assert rendered.count("<capability-catalog>") == 1
    assert rendered.endswith(
        f"<capability-catalog>\n{skill_section}\n{service_section}</capability-catalog>"
    )


@pytest.mark.parametrize("runtime,filesystem", tuple(product((False, True), repeat=2)))
def test_tool_template_keeps_only_selected_guidance(
    runtime: bool, filesystem: bool
) -> None:
    sections = (
        "<available-runnable-routes>Routes</available-runnable-routes>"
        if runtime
        else "",
        prompts.load("filesystem.md") if filesystem else "",
    )

    assert render_tool_instructions(sections[0], filesystem=filesystem) == "\n\n".join(
        item for item in sections if item
    )


def test_tool_template_does_not_escape_or_reinterpret_rendered_guidance() -> None:
    runnable = (
        '<routes>{"documentation":"quoted \\u003c and {{instructions}}"}</routes>'
        "\nUse &amp; literally. {{/instructions}}"
    )

    assert render_tool_instructions(runnable, filesystem=True) == (
        runnable + "\n\n" + prompts.load("filesystem.md")
    )
