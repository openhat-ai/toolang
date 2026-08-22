"""Shared Rich renderables for Script and Chat execution progress."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from rich.console import Console, ConsoleOptions, Group, RenderableType, RenderResult
from rich.markdown import CodeBlock, Heading, HorizontalRule, Markdown
from rich.rule import Rule
from rich.segment import Segment
from rich.style import Style
from rich.syntax import Syntax, SyntaxTheme, TokenType
from rich.text import Text
from rich.theme import Theme

from .formatting import display_width, split_hanging_prefix, truncate
from .types import ProgressBlock, ProgressRow, ProgressTone

_STYLES: dict[ProgressTone, str] = {
    "progress": "dim",
    "normal": "none",
    "active": "none",
    "error": "red",
    "warning": "yellow",
}
RUN_DIVIDER_WIDTH = 42
TERMINAL_MARKDOWN_THEME = Theme({"markdown.code": "bold"})

_ANSI_CODE_THEME = Syntax.get_theme("ansi_dark")


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


class _ProgressCodeTheme(SyntaxTheme):
    """Use terminal-owned ANSI colors on a consistent code surface."""

    def get_style_for_token(self, token_type: TokenType) -> Style:
        return _ANSI_CODE_THEME.get_style_for_token(token_type)

    def get_background_style(self) -> Style:
        return Style(color="bright_white", bgcolor="bright_black")


class _ProgressCodeBlock(CodeBlock):
    """Render fenced code with the shared terminal-native palette."""

    def __rich_console__(
        self,
        console: Console,
        options: ConsoleOptions,
    ) -> RenderResult:
        code = str(self.text).rstrip()
        yield Syntax(
            code,
            self.lexer_name,
            theme=_ProgressCodeTheme(),
            word_wrap=True,
            padding=1,
        )


class _ProgressMarkdown(Markdown):
    elements = {
        **Markdown.elements,
        "code_block": _ProgressCodeBlock,
        "fence": _ProgressCodeBlock,
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
    gap_before: bool,
) -> RenderableType:
    """Render one shared root Run footer with its leading separator."""

    footer = _RunFooter(
        run_id=run_id,
        status=status,
        facts=" · ".join(facts),
        max_width=max_width,
    )
    return Group(Text(), footer) if gap_before else footer


def terminal_status_style(status: str) -> str:
    """Return the shared terminal style for one completed execution status."""

    return {
        "succeeded": "dim",
        "failed": "red",
        "canceled": "yellow",
    }.get(status, "dim")


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
        divider_width = min(width, RUN_DIVIDER_WIDTH)
        title = f"{self.run_id} {self.status}"
        status_style = terminal_status_style(self.status)
        if divider_width < 5:
            divider = Text("•", style=status_style)
            if divider_width > 1:
                divider.append(" ", style=status_style)
            if divider_width > 2:
                divider.append(
                    truncate(title, divider_width - 2),
                    style=status_style,
                )
            divider.no_wrap = True
            yield divider
            return

        facts_indent = 2
        content_width = max(width - facts_indent, 1)
        fact_lines = Text(self.facts, style="dim").wrap(
            console,
            content_width,
            overflow="fold",
        ) or [Text("", style="dim")]
        for line in fact_lines:
            line.rstrip()
        title = truncate(title, max(divider_width - 4, 1))
        title_cells = display_width(title)
        top = Text()
        top.append("•", style=status_style)
        top.append(" ", style=status_style)
        top.append(title, style=status_style)
        top.append(" ", style=status_style)
        top.append(
            "─" * max(divider_width - title_cells - 3, 0),
            style=status_style,
        )
        top.no_wrap = True
        yield top

        for line in fact_lines:
            facts = Text(" " * facts_indent + line.plain, style="dim")
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
        preserve_background = console.color_system is not None
        while lines and not _line_has_content(
            lines[0], preserve_background=preserve_background
        ):
            lines.pop(0)
        while lines and not _line_has_content(
            lines[-1], preserve_background=preserve_background
        ):
            lines.pop()
        if not lines:
            yield Text(prefix.rstrip(), style=_STYLES[self.row.tone], no_wrap=True)
            return

        continuation = " " * prefix_width
        if self.row.gap_before:
            yield Text("", no_wrap=True)
        for index, line in enumerate(lines):
            rendered = Text(prefix if index == 0 else continuation)
            trimmed_line = _rstrip_unpainted(
                line,
                preserve_background=preserve_background,
            )
            for segment in trimmed_line:
                if segment.control or not segment.text:
                    continue
                rendered.append(
                    segment.text,
                    style=segment.style or _STYLES[self.row.tone],
                )
            if not trimmed_line:
                rendered.rstrip()
            rendered.no_wrap = True
            yield rendered


def _line_text(line: list[Segment]) -> str:
    return "".join(segment.text for segment in line if not segment.control)


def _line_has_content(
    line: list[Segment],
    *,
    preserve_background: bool,
) -> bool:
    return bool(_line_text(line).strip()) or (
        preserve_background
        and any(
            segment.style is not None and segment.style.bgcolor is not None
            for segment in line
            if not segment.control and segment.text
        )
    )


def _rstrip_unpainted(
    line: list[Segment],
    *,
    preserve_background: bool,
) -> list[Segment]:
    trimmed = list(line)
    while trimmed:
        segment = trimmed[-1]
        if segment.control or not segment.text:
            trimmed.pop()
            continue
        if (
            preserve_background
            and segment.style is not None
            and segment.style.bgcolor is not None
        ):
            break
        text = segment.text.rstrip()
        if text:
            trimmed[-1] = Segment(text, segment.style, segment.control)
            break
        trimmed.pop()
    return trimmed
