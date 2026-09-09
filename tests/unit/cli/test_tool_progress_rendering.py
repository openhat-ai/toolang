"""Shared terminal presentation keeps tool descriptions compact and data-free."""

from io import StringIO

import pytest
from rich.console import Console
from rich.style import Style

from toolang.cli.common.execution_progress import ProgressBlock, ProgressRow
from toolang.cli.common.execution_progress.formatting import display_width
from toolang.cli.common.execution_progress.rich_rendering import (
    progress_block_renderable,
)


@pytest.mark.parametrize("live", [False, True])
@pytest.mark.parametrize("prefix", ["", "  1 | #2 | "])
@pytest.mark.parametrize("runtime", [False, True])
@pytest.mark.parametrize("width", [16, 48, 120])
def test_tool_summary_is_one_line_with_dim_marker(width, runtime, prefix, live):
    marker = "✧" if runtime else "›"
    row = ProgressRow(
        f"{prefix}{marker} Reading repo:/很长的目录/" + "nested/" * 30,
        "progress",
        surface="tool_summary",
    )
    stream = StringIO()
    console = Console(file=stream, width=width, force_terminal=True)
    rendered = progress_block_renderable(
        ProgressBlock("tool", (row,)), live=live, max_width=width
    )
    segments = list(console.render(rendered))
    text = "".join(s.text for s in segments)
    assert len(text.splitlines()) == 1
    assert display_width(text.rstrip()) <= width
    assert text.rstrip().endswith("…")
    mark = next(s for s in segments if marker in s.text)
    assert mark.style is not None and mark.style.dim
    assert mark.style.color is None
    body = next(s for s in segments if "…" in s.text)
    assert body.style is not None and body.style.dim
    assert body.style.color is None


@pytest.mark.parametrize("tone", ["normal", "error", "warning", "active"])
@pytest.mark.parametrize("markdown", [False, True])
def test_model_marker_remains_normal_without_changing_content_style(tone, markdown):
    row = ProgressRow(
        "Model response" if markdown else "• Model response",
        tone,
        format="markdown" if markdown else "plain",
        prefix="• " if markdown else "",
    )
    console = Console(width=80)
    segments = list(
        console.render(
            progress_block_renderable(
                ProgressBlock("model", (row,)),
                live=False,
                max_width=80,
            )
        )
    )
    marker = next(s for s in segments if "•" in s.text)
    assert marker.style is None or (marker.style.color is None and not marker.style.dim)
    assert "Model response" in "".join(s.text for s in segments)


@pytest.mark.parametrize("live", [False, True])
@pytest.mark.parametrize("width", [8, 16, 80])
@pytest.mark.parametrize(
    ("marker", "tone", "surface"),
    [
        ("›", "progress", "tool_summary"),
        ("✧", "progress", "tool_summary"),
        ("•", "active", "none"),
        ("•", "error", "none"),
    ],
)
def test_lane_identity_styles_are_independent_of_activity(
    live, width, marker, tone, surface
):
    row = ProgressRow(f"  1 | #2 | {marker} Activity continues", tone, surface=surface)
    console = Console(width=width, force_terminal=True, _environ={})
    segments = list(
        console.render(
            progress_block_renderable(
                ProgressBlock("lane", (row,)), live=live, max_width=width
            )
        )
    )
    characters = [
        (char, segment.style or Style.null())
        for segment in segments
        for char in segment.text
    ]
    for index in (2, 4):
        assert characters[index][1].dim is True
        assert characters[index][1].color is None
    assert characters[6][0] == "#"
    assert characters[6][1].dim is False
    assert characters[6][1].color is None
    if width >= 16:
        assert characters[7][1].dim is False
        assert characters[9][1].dim is True
        body = next(style for char, style in characters if char == "A")
        assert bool(body.dim) is (tone == "progress")
        assert (body.color is not None) is (tone == "error")
