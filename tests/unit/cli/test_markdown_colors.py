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


@pytest.mark.parametrize("inline_background", [None, "#151515", "#efefef"])
@pytest.mark.parametrize("inherit", [False, True])
@pytest.mark.parametrize("code_style", [None, "italic yellow on magenta"])
def test_code_background_preserves_and_restores_console_theme(
    inherit: bool, code_style: str | None, inline_background: str | None
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
                    inline_code_background=inline_background,
                    code_foreground=None,
                )
            )
        )
        code = next(segment for segment in segments if segment.text == "value")
        emphasis = next(segment for segment in segments if segment.text == "emphasis")
        assert code.style == (original_code.style or Style()) + Style(
            bgcolor=inline_background or background
        )
        assert emphasis.style == original_emphasis.style
        assert list(console.render(Markdown(source))) == original


@pytest.mark.parametrize("width", [24, 60])
def test_inline_background_preserves_markdown_text_and_wrapping(width: int) -> None:
    console = Console(width=width)
    source = (
        "before `value` after long enough to wrap a narrow row\n\n"
        "- list `value`\n\n> quote `value`\n\n"
        "| Header |\n| --- |\n| `value` |\n\n```text\nblock\n```"
    )
    block = ProgressBlock(
        "response", (ProgressRow(source, "normal", format="markdown"),)
    )

    def render(inline: str):
        return list(
            console.render(
                progress_block_renderable(
                    block,
                    live=False,
                    max_width=width,
                    code_background="#f9f9f9",
                    inline_code_background=inline,
                    code_foreground=None,
                )
            )
        )

    original = render("#f9f9f9")
    updated = render("#efefef")
    assert "".join(s.text for s in updated) == "".join(s.text for s in original)
    values = [s for s in updated if s.text == "value"]
    assert len(values) == 4
    for segment in values:
        assert segment.style is not None
        assert segment.style.bgcolor == Style(bgcolor="#efefef").bgcolor
    fenced = next(s for s in updated if "block" in s.text)
    assert fenced.style is not None
    assert fenced.style.bgcolor == Style(bgcolor="#f9f9f9").bgcolor
