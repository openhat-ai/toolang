"""Shared Rich renderables for Script and Chat execution progress."""

from __future__ import annotations

from dataclasses import dataclass

from rich.console import Console, ConsoleOptions, Group, RenderableType, RenderResult
from rich.markdown import Heading, Markdown
from rich.segment import Segment
from rich.text import Text

from .formatting import display_width, split_hanging_prefix, truncate
from .types import ProgressBlock, ProgressRow, ProgressTone

_STYLES: dict[ProgressTone, str] = {
    "progress": "dim",
    "normal": "none",
    "active": "none",
    "error": "red",
    "warning": "yellow",
}


class _ProgressHeading(Heading):
    """Keep streamed headings aligned with the Step marker."""

    LEVEL_ALIGN = {
        "h1": "left",
        "h2": "left",
        "h3": "left",
        "h4": "left",
        "h5": "left",
        "h6": "left",
    }


class _ProgressMarkdown(Markdown):
    elements = {**Markdown.elements, "heading_open": _ProgressHeading}


def progress_block_renderable(
    block: ProgressBlock,
    *,
    live: bool,
    max_width: int,
) -> RenderableType:
    """Render one semantic progress block with shared wrapping and Markdown."""

    return Group(
        *(
            _MarkdownRow(row, max_width=max_width)
            if row.format == "markdown"
            else _PlainRow(row, live=live, max_width=max_width)
            for row in block.rows
        )
    )


@dataclass(frozen=True, slots=True)
class _PlainRow:
    row: ProgressRow
    live: bool
    max_width: int

    def __rich_console__(
        self,
        console: Console,
        options: ConsoleOptions,
    ) -> RenderResult:
        style = _STYLES[self.row.tone]
        width = max(1, min(options.max_width, self.max_width))
        if self.live and not self.row.wrap_live:
            yield Text(
                truncate(self.row.text, width),
                style=style,
                no_wrap=True,
            )
            return

        prefix, content = split_hanging_prefix(self.row.text)
        prefix_width = display_width(prefix)
        if prefix_width >= width:
            lines = Text(self.row.text, style=style).wrap(
                console,
                width,
                overflow="fold",
            )
            for line in lines:
                line.rstrip()
                yield Text(line.plain, style=style, no_wrap=True)
            return

        lines = Text(content, style=style).wrap(
            console,
            width - prefix_width,
            overflow="fold",
        )
        if not lines:
            lines.append(Text("", style=style))
        continuation = " " * prefix_width
        for index, line in enumerate(lines):
            line.rstrip()
            yield Text(
                f"{prefix if index == 0 else continuation}{line.plain}",
                style=style,
                no_wrap=True,
            )


@dataclass(frozen=True, slots=True)
class _MarkdownRow:
    row: ProgressRow
    max_width: int

    def __rich_console__(
        self,
        console: Console,
        options: ConsoleOptions,
    ) -> RenderResult:
        width = max(1, min(options.max_width, self.max_width))
        prefix = self.row.prefix
        prefix_width = display_width(prefix)
        content_width = max(1, width - prefix_width)
        segments = console.render(
            _ProgressMarkdown(self.row.text),
            options.update_width(content_width),
        )
        lines = list(Segment.split_lines(segments))
        while lines and not _line_text(lines[0]).strip():
            lines.pop(0)
        while lines and not _line_text(lines[-1]).strip():
            lines.pop()
        if not lines:
            yield Text(prefix.rstrip(), style=_STYLES[self.row.tone], no_wrap=True)
            return

        continuation = " " * prefix_width
        for index, line in enumerate(lines):
            rendered = Text(prefix if index == 0 else continuation)
            for segment in line:
                if segment.control or not segment.text:
                    continue
                rendered.append(
                    segment.text,
                    style=segment.style or _STYLES[self.row.tone],
                )
            rendered.rstrip()
            rendered.no_wrap = True
            yield rendered


def _line_text(line: list[Segment]) -> str:
    return "".join(segment.text for segment in line if not segment.control)
