"""Text agent messages share Chat's Markdown layout and terminal palette."""

import pytest
from rich.color import Color
from rich.console import Console

from toolang.cli.common.terminal_surfaces import (
    DARK_TERMINAL_SURFACES,
    LIGHT_TERMINAL_SURFACES,
)
from toolang.cli.toolang.commands.text.rendering import message_block
from toolang.teaming.schemas import Message


def render(source, *, width=60, surfaces=LIGHT_TERMINAL_SURFACES, sender="agent:alice"):
    console = Console(width=width, force_terminal=True, _environ={})
    block = message_block(
        Message.create(sender, source), "human:bryan", set(), width, surfaces
    )
    return list(console.render(block))


@pytest.mark.parametrize("width", [24, 60])
def test_agent_table_fills_body_width_and_preserves_long_tokens(width):
    token = "provider_resolver.resolve_catalog_providers"
    segments = render(f"| producer |\n| --- |\n| `{token}` |", width=width)
    text = "".join(segment.text for segment in segments)
    assert "…" not in text
    assert token in "".join(text.split())
    ruler = next(line for line in text.splitlines() if "─" in line)
    assert len(ruler.strip()) == width - min(8, width // 5) - 4
    assert all(len(line) <= width for line in text.splitlines())


def test_agent_headings_lists_and_rules_follow_chat_layout():
    source = "# Heading\n\n- outer\n  - inner\n\n1. first\n2. second\n\n---"
    lines = [
        line.rstrip() for line in "".join(s.text for s in render(source)).splitlines()
    ]
    assert "• alice" in lines
    assert "  Heading" in lines
    assert "  • outer" in lines
    assert "    • inner" in lines
    assert "  1 first" in lines
    assert "  2 second" in lines
    assert "  " + "─" * 48 in lines


@pytest.mark.parametrize("surfaces", [DARK_TERMINAL_SURFACES, LIGHT_TERMINAL_SURFACES])
def test_agent_inline_and_fenced_code_use_resolved_terminal_surfaces(surfaces):
    segments = render("Inline `value`\n\n```text\nblock\n```", surfaces=surfaces)
    inline = next(segment for segment in segments if segment.text == "value")
    fenced = next(segment for segment in segments if "block" in segment.text)
    assert inline.style is not None and fenced.style is not None
    assert inline.style.bgcolor == Color.parse(surfaces.inline_code_background)
    assert fenced.style.bgcolor == Color.parse(surfaces.code_background)
    assert fenced.style.color is None


def test_agent_links_do_not_emit_terminal_hyperlinks():
    segments = render("[label](https://example.com)")
    assert all(not segment.style or segment.style.link is None for segment in segments)
    assert "label" in "".join(segment.text for segment in segments)


def test_human_markdown_remains_literal():
    source = "# Heading **bold** `value`"
    segments = render(source, sender="human:bryan")
    assert source in "".join(segment.text for segment in segments)
    assert not any(segment.style and segment.style.bold for segment in segments)
