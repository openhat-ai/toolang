"""Shared terminal presentation keeps tool descriptions compact and data-free."""

from io import StringIO

import pytest
from rich.console import Console
from rich.style import Style

from toolang.cli.common.execution_progress import ProgressBlock, ProgressRow
from toolang.cli.common.execution_progress.formatting import display_width
from toolang.cli.common.execution_progress.rich_rendering import (
    PROGRESS_RIGHT_INSET,
    progress_block_renderable,
)


@pytest.mark.parametrize("live", [False, True])
@pytest.mark.parametrize("tone", ["active", "progress"])
@pytest.mark.parametrize("prefix", ["", "  1 | #2 | "])
@pytest.mark.parametrize("runtime", [False, True])
@pytest.mark.parametrize("width", [16, 48, 120])
def test_tool_summary_intensity_follows_activity_not_live_region(
    width, runtime, prefix, live, tone
):
    marker = "✧" if runtime else "›"
    row = ProgressRow(
        f"{prefix}{marker} Reading repo://很长的目录/" + "nested/" * 30,
        tone,
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
    right_inset = PROGRESS_RIGHT_INSET if not prefix else 0
    assert display_width(text.rstrip()) <= width - right_inset
    assert "…" not in text
    mark = next(s for s in segments if marker in s.text)
    body = next(s for s in segments if "R" in s.text)
    for segment in (mark, body):
        style = segment.style or Style.null()
        assert bool(style.dim) is (tone == "progress")
        assert not style.bold
        assert style.color is None


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
        ("›", "active", "tool_summary"),
        ("✧", "active", "tool_summary"),
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


@pytest.mark.parametrize(
    ("surface", "text", "tone"),
    [
        (
            "tool_summary",
            "› " + "Completed a long running tool operation " * 2,
            "progress",
        ),
        (
            "tool_error",
            "  " + "Tool call failed with a long diagnostic " * 2,
            "error",
        ),
    ],
)
def test_tool_progress_rows_reserve_the_right_output_inset(surface, text, tone):
    width = 40
    console = Console(width=width)
    rendered = progress_block_renderable(
        ProgressBlock("tool", (ProgressRow(text, tone, surface=surface),)),
        live=False,
        max_width=width,
    )
    segments = list(console.render(rendered))
    lines = "".join(segment.text for segment in segments).splitlines()

    assert lines
    assert all("…" not in line for line in lines)
    assert all(
        display_width(line.rstrip()) <= width - PROGRESS_RIGHT_INSET for line in lines
    )


@pytest.mark.parametrize("width", [1, 2, 3, 4])
@pytest.mark.parametrize(
    ("surface", "text", "visible_content"),
    [
        ("tool_summary", "› Operation failed", "O"),
        ("tool_error", "  Permission denied", "P"),
    ],
)
def test_narrow_tool_progress_preserves_one_content_cell(
    width, surface, text, visible_content
):
    console = Console(width=width)
    rendered = progress_block_renderable(
        ProgressBlock("tool", (ProgressRow(text, "error", surface=surface),)),
        live=False,
        max_width=width,
    )
    lines = "".join(segment.text for segment in console.render(rendered)).splitlines()

    assert len(lines) == 1
    assert visible_content in lines[0]
    assert "…" not in lines[0]
    assert display_width(lines[0].rstrip()) <= width
