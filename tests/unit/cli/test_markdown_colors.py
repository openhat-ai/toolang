"""Markdown color overrides preserve the caller's theme and rendering scope."""

import pytest
from rich.console import Console
from rich.markdown import Markdown
from rich.style import Style
from rich.theme import Theme

from toolang.cli.common.execution_progress import ProgressBlock, ProgressRow
from toolang.cli.common.execution_progress.rich_rendering import (
    progress_block_renderable,
)


@pytest.mark.parametrize("inherit", [False, True])
@pytest.mark.parametrize("code_style", [None, "italic yellow on magenta"])
def test_code_background_preserves_and_restores_console_theme(
    inherit: bool, code_style: str | None
) -> None:
    styles = {"markdown.paragraph": "green", "markdown.em": "italic"}
    if code_style is not None:
        styles["markdown.code"] = code_style
    console = Console(width=60, theme=Theme(styles, inherit=inherit))
    source = "before *emphasis* `value` after"
    original = list(console.render(Markdown(source)))
    original_code = next(segment for segment in original if segment.text == "value")
    original_emphasis = next(
        segment for segment in original if segment.text == "emphasis"
    )
    block = ProgressBlock(
        "response", (ProgressRow(source, "normal", format="markdown"),)
    )

    for background in ("#304050", "#f9f9f9"):
        segments = list(
            console.render(
                progress_block_renderable(
                    block,
                    live=False,
                    max_width=60,
                    code_background=background,
                    code_foreground=None,
                )
            )
        )
        code = next(segment for segment in segments if segment.text == "value")
        emphasis = next(segment for segment in segments if segment.text == "emphasis")
        assert code.style == (original_code.style or Style()) + Style(
            bgcolor=background
        )
        assert emphasis.style == original_emphasis.style
        assert list(console.render(Markdown(source))) == original
