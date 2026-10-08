"""Subtle agent header rules respect Text's existing layout and colors."""

import pytest
from rich.console import Console

from toolang.cli.common.terminal_surfaces import (
    DARK_TERMINAL_SURFACES,
    LIGHT_TERMINAL_SURFACES,
)
from toolang.cli.toolang.commands.text.rendering import message_block
from toolang.teaming.schemas import Message


@pytest.mark.parametrize("width", [24, 60, 120])
@pytest.mark.parametrize("own_message", [False, True])
@pytest.mark.parametrize("surfaces", [DARK_TERMINAL_SURFACES, LIGHT_TERMINAL_SURFACES])
def test_agent_rule_fills_header_without_changing_body_or_spacing(
    width, own_message, surfaces
):
    console = Console(width=width)
    segments = list(
        console.render(
            message_block(
                Message.create("agent:alice", "First.\n\nLast."),
                "agent:alice" if own_message else "human:bryan",
                set(),
                width,
                surfaces,
            )
        )
    )
    lines = "".join(segment.text for segment in segments).splitlines()
    gutter = min(8, width // 5)
    rule = "─" * (width - gutter - 4 - len("alice "))
    if own_message:
        assert lines[0] == " " * (gutter + 2) + rule + " alice •"
    else:
        assert lines[0] == "• alice " + rule + " " * (gutter + 2)
    assert [line.strip() for line in lines[1:]] == ["First.", "", "Last.", ""]
    rules = [segment for segment in segments if "─" in segment.text]
    assert rules
    for segment in rules:
        assert segment.style is not None and segment.style.dim is True
        assert segment.style.color is None and segment.style.bgcolor is None
        assert not segment.style.bold
    for segment in segments:
        if "alice" in segment.text or "•" in segment.text:
            assert segment.style is not None and segment.style.dim is False
            assert segment.style.color is not None


@pytest.mark.parametrize("own_message", [False, True])
@pytest.mark.parametrize(
    "name,width,has_rule",
    [
        *(("alice", width, False) for width in (1, 2, 3, 4, 12)),
        ("alice", 13, True),
        ("a_long_agent_name_that_needs_wrapping", 24, False),
        ("alice七", 15, False),
        ("alice七", 16, True),
        ("e\u0301cho", 12, True),
    ],
)
def test_rule_yields_space_to_full_names_using_terminal_cell_width(
    own_message, name, width, has_rule
):
    sender = f"agent:{name}"
    console = Console(width=width)
    block = message_block(
        Message.create(sender, "body"),
        sender if own_message else "human:bryan",
        set(),
        width,
        LIGHT_TERMINAL_SURFACES,
    )
    lines = console.render_lines(block)
    text = "".join(segment.text for line in lines for segment in line)
    assert ("─" in text) == has_rule
    assert name in "".join(text.split()).replace("•", "").replace("─", "")
    assert "…" not in text
    assert all(sum(segment.cell_length for segment in line) <= width for line in lines)


@pytest.mark.parametrize("own_message", [False, True])
def test_human_headers_have_no_rule(own_message):
    console = Console(width=60)
    block = message_block(
        Message.create("human:bryan", "body"),
        "human:bryan" if own_message else "human:visitor",
        set(),
        60,
        LIGHT_TERMINAL_SURFACES,
    )
    assert "─" not in "".join(segment.text for segment in console.render(block))
