"""Rich rendering helpers for terminal chat blocks."""

from __future__ import annotations

import shutil
import sys
from collections.abc import Sequence
from typing import TextIO

from prompt_toolkit.formatted_text import FormattedText
from rich.color import Color, ColorSystem
from rich.console import Console, RenderableType
from rich.segment import Segment
from rich.style import Style
from rich.text import Text
from wcwidth import wcswidth

CONTROL_BAR_BACKGROUND = "#3a3a3a"
START_CONTROL_ACCENT = "#8fd7ff"
STEER_CONTROL_ACCENT = "#d7b3ff"


def terminal_width(default: int = 100) -> int:
    return shutil.get_terminal_size((default, 24)).columns


def markdown_width() -> int:
    return min(100, max(40, terminal_width() - 4))


def chat_console(*, width: int | None = None, file: TextIO | None = None) -> Console:
    fixed_width = width or terminal_width()
    return Console(
        file=file,
        width=fixed_width,
        color_system="truecolor",
        force_terminal=True,
        legacy_windows=False,
        _environ={"COLUMNS": str(fixed_width), "LINES": "24"},
    )


def bar(
    segments: Sequence[tuple[str, str]],
    *,
    style: str = "white on grey23",
    width: int | None = None,
) -> Text:
    text = Text(style=style)
    for value, segment_style in segments:
        if value:
            text.append(value, style=segment_style)
    text.pad_right(max(0, (width or terminal_width()) - text.cell_len))
    return text


def render_segments(
    renderable: RenderableType | None, *, width: int | None = None
) -> list[Segment]:
    if renderable is None:
        return []
    console = chat_console(width=width)
    segments = list(console.render(renderable, console.options))
    if segments and segments[-1].text == "\n" and not segments[-1].control:
        segments.pop()
    return segments


def renderable_to_prompt_toolkit(renderable: RenderableType | None) -> FormattedText:
    fragments: list[tuple[str, str]] = []
    for segment in render_segments(renderable):
        if segment.control or not segment.text:
            continue
        fragments.append((rich_style_to_prompt_toolkit(segment.style), segment.text))
    return FormattedText(fragments)


def renderables_to_prompt_toolkit(
    renderables: Sequence[RenderableType | None],
    *,
    max_rows: int | None = None,
) -> FormattedText:
    """Render multiple live blocks into one optionally bounded viewport."""

    rows = _prompt_toolkit_rows(renderables)
    if max_rows is not None:
        if max_rows <= 0:
            rows = []
        elif len(rows) > max_rows:
            if max_rows == 1:
                rows = [rows[-1]]
            else:
                visible_rows = max_rows - 1
                hidden_rows = len(rows) - visible_rows
                rows = [
                    [("class:dim", f"… {hidden_rows} earlier live lines")],
                    *rows[-visible_rows:],
                ]

    fragments: list[tuple[str, str]] = []
    for index, row in enumerate(rows):
        if index:
            fragments.append(("", "\n"))
        fragments.extend(row)
    return FormattedText(fragments)


def renderables_height(renderables: Sequence[RenderableType | None]) -> int:
    """Return the number of rows occupied by adjacent live blocks."""

    return len(_prompt_toolkit_rows(renderables))


def _prompt_toolkit_rows(
    renderables: Sequence[RenderableType | None],
) -> list[list[tuple[str, str]]]:
    rows: list[list[tuple[str, str]]] = []
    for renderable in renderables:
        if renderable is None:
            continue
        block_rows: list[list[tuple[str, str]]] = [[]]
        has_content = False
        for segment in render_segments(renderable):
            if segment.control or not segment.text:
                continue
            has_content = True
            style = rich_style_to_prompt_toolkit(segment.style)
            parts = segment.text.split("\n")
            for index, part in enumerate(parts):
                if index:
                    block_rows.append([])
                if part:
                    block_rows[-1].append((style, part))
        if has_content:
            if len(block_rows) > 1 and not block_rows[-1]:
                block_rows.pop()
            rows.extend(block_rows)
    return rows


def write_renderable(
    renderable: RenderableType | None, *, hide_cursor: bool = True
) -> None:
    write_renderables([renderable], hide_cursor=hide_cursor)


def write_renderables(
    renderables: Sequence[RenderableType | None], *, hide_cursor: bool = True
) -> None:
    pending = [renderable for renderable in renderables if renderable is not None]
    if not pending:
        return
    if hide_cursor:
        sys.stdout.write("\x1b[?25l")
    try:
        sys.stdout.write(renderables_output(pending))
        sys.stdout.flush()
    finally:
        if hide_cursor:
            sys.stdout.write("\x1b[?25h")
            sys.stdout.flush()


def renderables_output(renderables: Sequence[RenderableType | None]) -> str:
    """Render stable terminal blocks into one ANSI-encoded write."""

    return "".join(
        _renderable_output(renderable)
        for renderable in renderables
        if renderable is not None
    )


def _renderable_output(renderable: RenderableType) -> str:
    output = "".join(
        segment.style.render(
            segment.text,
            color_system=ColorSystem.TRUECOLOR,
            legacy_windows=False,
        )
        if segment.style is not None
        else segment.text
        for segment in render_segments(renderable)
        if not segment.control
    )
    return output if not output or output.endswith("\n") else f"{output}\n"


def rich_style_to_prompt_toolkit(style: Style | None) -> str:
    if style is None:
        return ""
    parts: list[str] = []
    if style.color:
        parts.append(_prompt_toolkit_color(style.color))
    if style.bgcolor:
        parts.append(f"bg:{_prompt_toolkit_color(style.bgcolor)}")
    if style.bold:
        parts.append("bold")
    if style.italic:
        parts.append("italic")
    if style.underline:
        parts.append("underline")
    if style.dim:
        parts.append("dim")
    if style.reverse:
        parts.append("reverse")
    return " ".join(parts)


def display_len(text: str) -> int:
    return max(0, wcswidth(text))


def summarize(message: str, *, width: int = 72) -> str:
    text = " ".join(message.split())
    return text if len(text) <= width else f"{text[: width - 3].rstrip()}..."


def progress_tail(line: str) -> str:
    return line if line.rstrip().endswith("...") else f"{line}..."


def truncate_display(text: str, *, width: int) -> str:
    if width <= 0 or display_len(text) <= width:
        return text
    ellipsis = "..."
    if width <= len(ellipsis):
        return ellipsis[:width]
    limit = width - len(ellipsis)
    pieces: list[str] = []
    used = 0
    for char in text:
        char_width = display_len(char)
        if used + char_width > limit:
            break
        pieces.append(char)
        used += char_width
    return f"{''.join(pieces).rstrip()}{ellipsis}"


def _prompt_toolkit_color(color: Color) -> str:
    triplet = color.get_truecolor()
    return f"#{triplet.red:02x}{triplet.green:02x}{triplet.blue:02x}"
