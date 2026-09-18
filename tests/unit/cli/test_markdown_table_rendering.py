"""Markdown tables fill the progress width without dropping cell content."""

from rich.console import Console

from toolang.cli.common.execution_progress import ProgressBlock, ProgressRow
from toolang.cli.common.execution_progress.formatting import display_width
from toolang.cli.common.execution_progress.rich_rendering import (
    progress_block_renderable,
)

PREFIX = "• "

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


def _render(source: str, *, width: int) -> str:
    row = ProgressRow(source, "normal", format="markdown", prefix=PREFIX)
    console = Console(width=width, force_terminal=True, _environ={})
    segments = console.render(
        progress_block_renderable(
            ProgressBlock("model", (row,)), live=False, max_width=width
        )
    )
    return "".join(segment.text for segment in segments if not segment.control)


def _ruler(text: str) -> str:
    return next(line for line in text.splitlines() if "─" in line)


def test_narrow_markdown_table_fills_the_progress_width():
    text = _render(NARROW_TABLE, width=60)
    assert display_width(_ruler(text)) == 60
    assert all(display_width(line) <= 60 for line in text.splitlines())


def test_wide_markdown_table_fills_the_progress_width():
    text = _render(WIDE_TABLE, width=60)
    assert display_width(_ruler(text)) == 60
    assert all(display_width(line) <= 60 for line in text.splitlines())


def test_markdown_table_has_no_padding_outside_its_columns():
    # The box edges that Rich reserves per side would add a blank cell
    # between the row prefix and the first column.
    text = _render(NARROW_TABLE, width=60)
    assert text.splitlines()[0].startswith(f"{PREFIX}layer")


def test_markdown_table_folds_cell_content_instead_of_dropping_it():
    text = _render(FOLDING_TABLE, width=60)
    assert "…" not in text
    assert "provider_resolver.resolve_catalog_providers" in "".join(text.split())
