"""Agent dividers separate full-width messages while preserving sender colors."""

import pytest
from rich.color import Color
from rich.console import Console

from toolang.cli.common.terminal_surfaces import (
    DARK_TERMINAL_SURFACES,
    LIGHT_TERMINAL_SURFACES,
)
from toolang.cli.toolang.commands.talk.rendering import message_block
from toolang.teaming.schemas import Message


@pytest.mark.parametrize("width", [24, 60, 120])
@pytest.mark.parametrize("own_message", [False, True])
@pytest.mark.parametrize("surfaces", [DARK_TERMINAL_SURFACES, LIGHT_TERMINAL_SURFACES])
def test_agent_rule_follows_body_without_a_gap_and_preserves_block_spacing(
    width, own_message, surfaces
):
    console = Console(width=width)
    segments = list(
        console.render(
            message_block(
                Message.create("agent:alice", "First.\n\nLast."),
                "agent:alice" if own_message else "human:bryan",
                width,
                surfaces,
            )
        )
    )
    lines = "".join(segment.text for segment in segments).splitlines()
    gutter = min(8, width // 5) if own_message else 0
    if own_message:
        assert lines[0] == " " * (width - len("alice •")) + "alice •"
    else:
        assert lines[0] == "• alice" + " " * (width - len("• alice"))
    assert [line.strip() for line in lines[1:-2]] == ["First.", "", "Last."]
    assert lines[-2] == " " * (gutter + 2) + "┄" * (width - gutter - 2)
    assert lines[-1] == " " * width
    rules = [segment for segment in segments if "┄" in segment.text]
    assert rules
    for segment in rules:
        assert segment.style is not None and segment.style.dim is True
        assert segment.style.color == Color.parse("bright_black")
        assert segment.style.bgcolor is None
        assert not segment.style.bold
    for segment in segments:
        if "alice" in segment.text or "•" in segment.text:
            assert segment.style is not None and segment.style.dim is False
            assert segment.style.color is not None


@pytest.mark.parametrize("width", [24, 60])
@pytest.mark.parametrize("own_message", [False, True])
@pytest.mark.parametrize(
    "body",
    [
        "First.\n\nLast.",
        "First.\n\n---",
        "```text\nfirst\n\nlast\n```",
        "    first\n    last",
        "- first\n- last",
        "| heading |\n| --- |\n| last |",
        "```text\n\n```",
        "<!-- comment -->",
    ],
)
def test_agent_divider_touches_the_last_visible_markdown_row(body, width, own_message):
    console = Console(width=width)
    block = message_block(
        Message.create("agent:alice", body),
        "agent:alice" if own_message else "human:bryan",
        width,
        LIGHT_TERMINAL_SURFACES,
    )
    lines = "".join(segment.text for segment in console.render(block)).splitlines()
    assert lines[-2].strip().startswith("┄")
    assert lines[-3].strip(), "Markdown must not leave an empty row before the divider"
    assert not lines[-1].strip()


@pytest.mark.parametrize("own_message", [False, True])
@pytest.mark.parametrize(
    "name,width",
    [
        *(("alice", width) for width in (1, 2, 3, 4, 12)),
        ("alice", 13),
        ("a_long_agent_name_that_needs_wrapping", 24),
        ("alice七", 15),
        ("alice七", 16),
        ("e\u0301cho", 12),
    ],
)
def test_rule_follows_wrapped_names_and_body_within_terminal_cell_width(
    own_message, name, width
):
    sender = f"agent:{name}"
    console = Console(width=width)
    block = message_block(
        Message.create(sender, "body"),
        sender if own_message else "human:bryan",
        width,
        LIGHT_TERMINAL_SURFACES,
    )
    lines = console.render_lines(block)
    text_lines = ["".join(segment.text for segment in line) for line in lines]
    assert text_lines[-2].strip(" ") == "┄" * text_lines[-2].count("┄")
    assert text_lines[-2].endswith("┄")
    assert name + "body" == "".join("".join(text_lines[:-2]).split()).replace("•", "")
    text = "".join(text_lines)
    assert "…" not in text
    assert all(sum(segment.cell_length for segment in line) <= width for line in lines)


@pytest.mark.parametrize("own_message", [False, True])
def test_human_headers_have_no_rule(own_message):
    console = Console(width=60)
    block = message_block(
        Message.create("human:bryan", "body"),
        "human:bryan" if own_message else "human:visitor",
        60,
        LIGHT_TERMINAL_SURFACES,
    )
    assert "┄" not in "".join(segment.text for segment in console.render(block))


@pytest.mark.parametrize("sender", ["agent:alice", "human:visitor"])
@pytest.mark.parametrize("width", [24, 60, 120])
def test_left_message_body_uses_full_available_width(sender, width):
    console = Console(width=width)
    body = "x" * (width - 4)
    block = message_block(
        Message.create(sender, body),
        "human:bryan",
        width,
        LIGHT_TERMINAL_SURFACES,
    )
    lines = "".join(segment.text for segment in console.render(block)).splitlines()
    assert "  " + body + "  " in lines


def test_short_left_human_bubble_fills_available_width():
    console = Console(width=60)
    block = message_block(
        Message.create("human:visitor", "short"),
        "human:bryan",
        60,
        LIGHT_TERMINAL_SURFACES,
    )
    background_widths = [
        sum(
            segment.cell_length
            for segment in row
            if segment.style and segment.style.bgcolor is not None
        )
        for row in console.render_lines(block)
    ]
    assert background_widths == [0, 60, 60, 60, 0]
