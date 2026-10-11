"""Progress Markdown follows the progress layout for tables and lists."""

import pytest
from rich.color import Color
from rich.console import Console
from rich.segment import Segment

from toolang.cli.common.execution_progress import ProgressBlock, ProgressRow
from toolang.cli.common.execution_progress.formatting import display_width
from toolang.cli.common.execution_progress.rich_rendering import (
    progress_block_renderable,
)

PREFIX = "• "
CONTINUATION = "  "

NARROW_TABLE = "| layer | meaning |\n| --- | --- |\n| one | two |\n"

WIDE_TABLE = (
    "| layer | kind | producer | consumer | meaning |\n"
    "| --- | --- | --- | --- | --- |\n"
    "| assembly | `ResolvedProvider` / `ResolvedModel` |"
    " `provider_resolver.resolve_catalog_providers`（仅 setup 调用） |"
    " selection | adapter name, api, env rules, ready |\n"
)

# One column keeps folded chunks adjacent in every rendered line, so the
# whitespace-stripped text reconstructs the token. Neighbouring columns
# interleave on shared lines, which would split it.
FOLDING_TABLE = (
    "| producer |\n"
    "| --- |\n"
    "| `provider_resolver.resolve_catalog_providers`（仅 setup 调用） |\n"
)


def _render(
    source: str, *, width: int, max_width: int | None = None, live: bool = False
) -> str:
    row = ProgressRow(source, "normal", format="markdown", prefix=PREFIX)
    console = Console(width=width, force_terminal=True, _environ={})
    segments = console.render(
        progress_block_renderable(
            ProgressBlock("model", (row,)),
            live=live,
            max_width=width if max_width is None else max_width,
        )
    )
    return "".join(segment.text for segment in segments if not segment.control)


def _ruler(text: str) -> str:
    return next(line for line in text.splitlines() if "─" in line)


def test_narrow_markdown_table_fills_the_progress_width():
    text = _render(NARROW_TABLE, width=60)
    assert display_width(_ruler(text)) == 58
    assert all(display_width(line) <= 58 for line in text.splitlines())


def test_wide_markdown_table_fills_the_progress_width():
    text = _render(WIDE_TABLE, width=60)
    assert display_width(_ruler(text)) == 58
    assert all(display_width(line) <= 58 for line in text.splitlines())


def test_markdown_table_has_no_padding_outside_its_columns():
    # The box edges that Rich reserves per side would add a blank cell
    # between the row prefix and the first column.
    text = _render(NARROW_TABLE, width=60)
    assert text.splitlines()[0].startswith(f"{PREFIX}layer")


def test_markdown_table_folds_cell_content_instead_of_dropping_it():
    text = _render(FOLDING_TABLE, width=60)
    assert "…" not in text
    assert "provider_resolver.resolve_catalog_providers" in "".join(text.split())


def test_bullet_list_markers_start_at_the_progress_prefix():
    text = _render("Seams:\n\n- ① first\n- ② second\n", width=60)
    assert "  • ① first" in text.splitlines()
    assert "  • ② second" in text.splitlines()


def test_bullet_list_content_wraps_under_its_marker():
    text = _render("Seams:\n\n- " + "alpha " * 30 + "\n", width=60)
    lines = text.splitlines()
    marker_line = next(line for line in lines if line.startswith("  • alpha"))
    wrapped = lines[lines.index(marker_line) + 1]
    # Continuation lines align with the marker's content, not its bullet.
    assert wrapped.startswith(f"{CONTINUATION}  alpha")


def test_block_quote_content_fills_the_progress_width():
    text = _render("Intro:\n\n> " + "x" * 100 + "\n", width=60)
    lines = text.splitlines()
    assert any(line.startswith(f"{CONTINUATION}▌ x") for line in lines)
    assert max(display_width(line) for line in lines) == 58


def test_nested_block_quotes_fill_the_progress_width():
    text = _render("Intro:\n\n> outer\n>\n> > " + "x" * 100 + "\n", width=60)
    lines = text.splitlines()
    assert any(line.startswith(f"{CONTINUATION}▌ ▌ x") for line in lines)
    assert max(display_width(line) for line in lines) == 58


@pytest.mark.parametrize("live", [False, True])
@pytest.mark.parametrize(("width", "max_width"), [(24, 120), (80, 60)])
@pytest.mark.parametrize(
    "source",
    [
        "x" * 100,
        "界" * 100,
        "- " + "x" * 100,
        "> " + "x" * 100,
        NARROW_TABLE,
        "---",
    ],
    ids=["paragraph", "cjk", "list", "quote", "table", "rule"],
)
def test_markdown_keeps_two_cells_at_the_effective_right_edge(
    source, width, max_width, live
):
    text = _render(source, width=width, max_width=max_width, live=live)
    assert max(display_width(line) for line in text.splitlines()) == (
        min(width, max_width) - 2
    )


@pytest.mark.parametrize("live", [False, True])
@pytest.mark.parametrize(("width", "max_width"), [(24, 120), (80, 60)])
@pytest.mark.parametrize("container", ["", "> ", "- "])
@pytest.mark.parametrize("fenced", [False, True])
def test_code_block_reaches_right_edge_with_existing_padding(
    width, max_width, live, container, fenced
):
    effective_width = min(width, max_width)
    left = len(PREFIX) + len(container)
    code = "x" * (effective_width - left - 4)
    source = f"```text\n{code}\n```" if fenced else f"    {code}"
    if container:
        continuation = container if container == "> " else "  "
        source = container + source.replace("\n", "\n" + continuation)
    row = ProgressRow(source, "normal", format="markdown", prefix=PREFIX)
    console = Console(width=width)
    lines = list(
        Segment.split_lines(
            console.render(
                progress_block_renderable(
                    ProgressBlock("model", (row,)), live=live, max_width=max_width
                )
            )
        )
    )
    painted = [
        [(character, segment.style) for segment in line for character in segment.text]
        for line in lines
        if any(s.style and s.style.bgcolor for s in line)
    ]
    assert len(painted) == 3
    assert "".join(c for c, _ in painted[1][left:]) == f"  {code}  "
    assert not "".join(c for c, _ in painted[0][left:]).strip()
    assert not "".join(c for c, _ in painted[2][left:]).strip()
    for line in painted:
        assert len(line) == effective_width
        assert all(
            style and style.bgcolor == Color.parse("bright_black")
            for _, style in line[left:]
        )


def test_nested_list_markers_add_two_cells_per_level():
    text = _render("Seams:\n\n- outer\n  - inner\n    - deepest\n", width=60)
    assert "  • outer" in text.splitlines()
    assert "    • inner" in text.splitlines()
    assert "      • deepest" in text.splitlines()


def test_ordered_list_numbers_start_at_the_progress_prefix():
    text = _render("Steps:\n\n1. first\n2. second\n", width=60)
    assert "  1 first" in text.splitlines()
    assert "  2 second" in text.splitlines()


def test_ordered_list_numbers_keep_multi_digit_alignment():
    source = "Steps:\n\n" + "".join(f"{number}. item\n" for number in range(1, 12))
    text = _render(source, width=60)
    assert "   1 item" in text.splitlines()
    assert "  10 item" in text.splitlines()
