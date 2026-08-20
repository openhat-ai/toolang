"""Shared Rich renderables for Script and Chat execution progress."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from rich.console import Console, ConsoleOptions, Group, RenderableType, RenderResult
from rich.markdown import Heading, HorizontalRule, Markdown
from rich.rule import Rule
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


class _ProgressHorizontalRule(HorizontalRule):
    """Render a quiet Unicode divider instead of Rich's ASCII hyphens."""

    def __rich_console__(
        self,
        console: Console,
        options: ConsoleOptions,
    ) -> RenderResult:
        del console, options
        yield Rule(style="dim", characters="─")
        yield Text()


class _ProgressMarkdown(Markdown):
    elements = {
        **Markdown.elements,
        "heading_open": _ProgressHeading,
        "hr": _ProgressHorizontalRule,
    }


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


def run_footer_renderable(
    *,
    run_id: str,
    status: str,
    facts: Sequence[str],
    max_width: int,
) -> RenderableType:
    """Render one shared root Run footer."""

    return _RunFooter(
        run_id=run_id,
        status=status,
        facts=" · ".join(facts),
        max_width=max_width,
    )


@dataclass(frozen=True, slots=True)
class _RunFooter:
    run_id: str
    status: str
    facts: str
    max_width: int

    def __rich_console__(
        self,
        console: Console,
        options: ConsoleOptions,
    ) -> RenderResult:
        width = max(1, min(options.max_width, self.max_width))
        title = f"{self.run_id} {self.status}"
        if width < 5:
            yield Text(truncate(title, width), style="dim", no_wrap=True)
            return

        caption_style = {
            "succeeded": "green",
            "failed": "red",
            "canceled": "yellow",
        }.get(self.status, "dim")
        rule_style = "dim"
        facts_indent = 2
        content_width = max(width - facts_indent, 1)
        fact_lines = Text(self.facts, style="dim").wrap(
            console,
            content_width,
            overflow="fold",
        ) or [Text("", style="dim")]
        for line in fact_lines:
            line.rstrip()
        facts_width = max(display_width(line.plain) for line in fact_lines)
        footer_width = min(
            width,
            max(facts_width + facts_indent, display_width(title) + 4),
        )
        title = truncate(title, max(footer_width - 4, 1))
        title_cells = display_width(title)
        top = Text()
        top.append("╶ ", style=rule_style)
        top.append(title, style=caption_style)
        top.append(" ", style=rule_style)
        top.append("─" * max(footer_width - title_cells - 3, 0), style=rule_style)
        top.no_wrap = True
        yield top

        for line in fact_lines:
            plain = line.plain
            facts = Text(" " * facts_indent + plain, style="dim")
            facts.append(
                " " * (footer_width - facts_indent - display_width(plain)),
                style="dim",
            )
            facts.no_wrap = True
            yield facts


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
        if self.row.gap_before:
            yield Text("", no_wrap=True)
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
