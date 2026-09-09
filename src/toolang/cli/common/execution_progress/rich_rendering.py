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

from .formatting import display_width, split_hanging_prefix, truncate, wrap_display
from .types import ProgressBlock, ProgressRow, ProgressTone

_STYLES: dict[ProgressTone, str] = {
    "progress": "dim",
    "normal": "none",
    "active": "none",
    "error": "red",
    "warning": "yellow",
}
RUN_DIVIDER_WIDTH = 42
TERMINAL_MARKDOWN_THEME = Theme({"markdown.code": "bold cyan"})
_SCRIPT_CODE_BACKGROUND = "bright_black"
_SCRIPT_CODE_FOREGROUND = "bright_white"

_ANSI_CODE_THEME = Syntax.get_theme("ansi_dark")


class _ProgressCodeTheme(SyntaxTheme):
    """Use terminal-owned ANSI token colors on one configured surface."""

    def __init__(self, *, background: str, foreground: str | None) -> None:
        self._background = background
        self._foreground = foreground

    def get_style_for_token(self, token_type: TokenType) -> Style:
        return _ANSI_CODE_THEME.get_style_for_token(token_type)

    def get_background_style(self) -> Style:
        return Style(color=self._foreground, bgcolor=self._background)


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


class _ProgressCodeBlock(CodeBlock):
    """Render fenced code with the shared terminal-native palette."""

    def __rich_console__(
        self,
        console: Console,
        options: ConsoleOptions,
    ) -> RenderResult:
        code = str(self.text).rstrip()
        foreground, separator, background = self.theme.partition("|")
        if not separator:
            background = self.theme
        yield Syntax(
            code,
            self.lexer_name,
            theme=_ProgressCodeTheme(
                background=background,
                foreground=foreground or None,
            ),
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
    code_background: str = _SCRIPT_CODE_BACKGROUND,
    code_foreground: str | None = _SCRIPT_CODE_FOREGROUND,
) -> RenderableType:
    """Render one semantic progress block with shared wrapping and Markdown."""

    rows = (
        _row_renderable(
            row,
            live=live,
            max_width=max_width,
            code_background=code_background,
            code_foreground=code_foreground,
        )
        for row in block.rows
    )
    return Group(Text(), *rows) if block.gap_before else Group(*rows)


def _row_renderable(
    row: ProgressRow,
    *,
    live: bool,
    max_width: int,
    code_background: str,
    code_foreground: str | None,
) -> RenderableType:
    if row.leader in {"hyphen", "handoff"}:
        renderable: RenderableType = _HyphenDividerRow(row, max_width=max_width)
    elif row.format == "plain" and row.right_text:
        renderable = _TwoEndedPlainRow(row, live=live, max_width=max_width)
    elif row.format == "markdown":
        renderable = _MarkdownRow(
            row,
            max_width=max_width,
            code_background=code_background,
            code_foreground=code_foreground,
        )
    else:
        renderable = _PlainRow(row, live=live, max_width=max_width)
    return renderable


def run_footer_renderable(
    *,
    run_id: str,
    operation: str | None = None,
    status: str,
    facts: Sequence[str],
    max_width: int,
    gap_before: bool,
) -> RenderableType:
    """Render one shared root Run footer with optional leading spacing."""

    footer = _RunFooter(
        run_id=run_id,
        operation=operation,
        status=status,
        facts=tuple(facts),
        max_width=max_width,
    )
    return Group(Text(), footer) if gap_before else footer


def terminal_status_style(status: str) -> str:
    """Return the shared terminal style for one completed execution status."""

    return _terminal_status_color(status) or "dim"


def _terminal_status_color(status: str) -> str | None:
    return {
        "failed": "red",
        "canceled": "yellow",
    }.get(status)


def _wrap_run_footer_facts(
    *,
    facts: tuple[str, ...],
    console: Console,
    width: int,
) -> list[Text]:
    lines: list[Text] = []
    for fact in facts:
        if not lines:
            lines.extend(Text(fact).wrap(console, width, overflow="fold") or [Text()])
            continue
        current = lines[-1]
        separator = " · "
        if display_width(current.plain + separator + fact) <= width:
            current.append(separator + fact)
            continue
        fact_lines = list(Text(fact).wrap(console, width, overflow="fold")) or [Text()]
        lines.extend(fact_lines)
    for line in lines:
        line.rstrip()
    return lines


@dataclass(frozen=True, slots=True)
class _RunFooter:
    run_id: str
    operation: str | None
    status: str
    facts: tuple[str, ...]
    max_width: int

    def __rich_console__(
        self,
        console: Console,
        options: ConsoleOptions,
    ) -> RenderResult:
        width = max(1, min(options.max_width, self.max_width))
        title = (
            f"{self.run_id}: {self.operation} {self.status}"
            if self.operation is not None
            else f"{self.run_id} {self.status}"
        )
        marker_style = _terminal_status_color(self.status) or "none"
        title_style = "dim"
        prefix = "∎ "
        prefix_width = display_width(prefix)
        if width <= prefix_width:
            line = Text(no_wrap=True)
            line.append(prefix, style=marker_style)
            line.append(title, style=title_style)
            line.truncate(width, overflow="ellipsis")
            yield line
            return

        title_text = f"{prefix}{title}"
        facts_text = " · ".join(self.facts)
        separating_width = width - display_width(title_text) - display_width(facts_text)
        if facts_text and separating_width >= 2:
            line = Text(no_wrap=True)
            line.append(prefix, style=marker_style)
            line.append(title, style=title_style)
            line.append(" " * separating_width)
            line.append(facts_text, style="dim")
            yield line
            return

        continuation = " " * prefix_width
        title_lines = Text(title).wrap(
            console,
            width - prefix_width,
            overflow="fold",
        ) or [Text()]
        for index, title_line in enumerate(title_lines):
            line = Text(no_wrap=True)
            line.append(
                prefix if index == 0 else continuation,
                style=marker_style if index == 0 else "none",
            )
            line.append(title_line.plain, style=title_style)
            yield line

        fact_lines = _wrap_run_footer_facts(
            facts=self.facts,
            console=console,
            width=width - prefix_width,
        )
        for fact_line in fact_lines:
            line = Text(no_wrap=True)
            line.append(continuation)
            line.append(fact_line.plain, style="dim")
            yield line


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
        if self.row.surface in {"tool_summary", "tool_error"} or (
            self.live and not self.row.wrap_live
        ):
            text = _plain_text(self.row.text, style)
            shortened = truncate(self.row.text, width)
            if shortened != self.row.text:
                text = text[: len(shortened) - 1]
                text.append("…", style=style)
            yield text
            return

        prefix, content = split_hanging_prefix(self.row.text)
        prefix_width = display_width(prefix)
        if prefix_width >= width:
            lines = _plain_text(self.row.text, style).wrap(
                console,
                width,
                overflow="fold",
                no_wrap=False,
            )
            for line in lines:
                line.rstrip()
                yield line
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
            yield _plain_text(
                f"{prefix if index == 0 else continuation}{line.plain}",
                style,
            )


def _plain_text(value: str, style: str) -> Text:
    """Style lane identities and activity independently, keeping bullets normal."""

    text = Text(no_wrap=True)
    prefix, content = split_hanging_prefix(value)
    lane, separator, rest = prefix.partition(" | ")
    item, closing, marker = rest.partition(" | ")
    if separator and closing and lane.strip().isdigit() and item.startswith("#"):
        text.append(lane + separator, style="dim")
        text.append(item, style="not dim")
        text.append(closing, style="dim")
        prefix = marker
    for char in prefix:
        text.append(char, style="none" if char == "•" else style)
    text.append(content, style=style)
    return text


@dataclass(frozen=True, slots=True)
class _TwoEndedPlainRow:
    """Render a left value and a complete right value within one row width."""

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
        prefix, left = split_hanging_prefix(self.row.text)
        prefix_width = display_width(prefix)
        right = self.row.right_text
        if prefix_width < width:
            content_width = width - prefix_width
            separating_width = (
                content_width - display_width(left) - display_width(right)
            )
            if separating_width >= 2:
                yield Text(
                    f"{prefix}{left}{' ' * separating_width}{right}",
                    style=style,
                    no_wrap=True,
                )
                return

        yield from _PlainRow(
            self.row,
            live=self.live,
            max_width=self.max_width,
        ).__rich_console__(console, options)

        if prefix_width < width:
            path_prefix = prefix
            path_width = width - prefix_width
        else:
            path_prefix = ""
            path_width = width
        path_lines = Text(right).wrap(
            console,
            path_width,
            overflow="fold",
        ) or [Text()]
        for path_line in path_lines:
            path_line.rstrip()
            padding = " " * max(0, path_width - display_width(path_line.plain))
            yield Text(
                f"{path_prefix}{padding}{path_line.plain}",
                style=style,
                no_wrap=True,
            )


@dataclass(frozen=True, slots=True)
class _HyphenDividerRow:
    """Render Run and execute boundaries with elastic borders."""

    row: ProgressRow
    max_width: int

    def __rich_console__(
        self,
        console: Console,
        options: ConsoleOptions,
    ) -> RenderResult:
        del console
        width = max(1, min(options.max_width, self.max_width))
        if self.row.right_status:
            yield from self._footer(width)
        else:
            yield from self._header(width)

    def _header(self, width: int) -> RenderResult:
        caption = self.row.text.removeprefix("---  ")
        yield from self._left_boundary(
            width,
            prefix="---  " if self.row.leader == "handoff" else "╓ ",
            character="-" if self.row.leader == "handoff" else "─",
            content=caption,
            border_style="dim",
        )

    @staticmethod
    def _left_boundary(
        width: int,
        *,
        prefix: str,
        character: str,
        content: str,
        border_style: str,
    ) -> RenderResult:
        prefix_width = display_width(prefix)
        minimum = 3
        gap = " "
        available = width - prefix_width - display_width(content) - display_width(gap)
        if available >= minimum:
            line = Text(no_wrap=True)
            line.append(prefix, style=border_style)
            line.append(content, style="dim")
            line.append(gap, style="dim")
            line.append(character * available, style=border_style)
            yield line
            return

        if width <= prefix_width:
            yield Text(prefix[:width], style=border_style, no_wrap=True)
            for content_line in wrap_display(content, width):
                yield Text(content_line, style="dim", no_wrap=True)
            yield Text(character * width, style=border_style, no_wrap=True)
            return

        indent = " " * prefix_width
        content_width = max(1, width - display_width(indent))
        lines = wrap_display(content, content_width)
        for index, content_line in enumerate(lines):
            if index == len(lines) - 1:
                remaining = width - prefix_width - display_width(content_line) - 1
                if remaining >= minimum:
                    line = Text(no_wrap=True)
                    if index == 0:
                        line.append(prefix, style=border_style)
                    else:
                        line.append(indent, style="dim")
                    line.append(content_line, style="dim")
                    line.append(" ", style="dim")
                    line.append(character * remaining, style=border_style)
                    yield line
                    return
            line = Text(no_wrap=True)
            if index == 0:
                line.append(prefix, style=border_style)
            else:
                line.append(indent, style="dim")
            line.append(content_line, style="dim")
            yield line
        leader_width = max(0, width - display_width(indent))
        if leader_width:
            line = Text(indent, style="dim", no_wrap=True)
            line.append(character * leader_width, style=border_style)
            yield line

    def _footer(self, width: int) -> RenderResult:
        prefix = "╙ "
        prefix_width = display_width(prefix)
        border_style = _terminal_status_color(self.row.right_status) or "dim"
        facts = " · ".join(self.row.facts)
        left = f"{prefix}{facts}"
        right = self.row.right_status
        if self.row.right_identity:
            right = f"{right} {self.row.right_identity}"
        left_gap = " " if facts else ""
        right_gap = " " if right else ""
        leader_width = (
            width
            - display_width(left)
            - display_width(left_gap)
            - display_width(right_gap)
            - display_width(right)
        )
        if leader_width >= 3:
            line = Text(no_wrap=True)
            line.append(prefix[0], style=border_style)
            line.append(prefix[1:], style="dim")
            line.append(facts, style="dim")
            line.append(left_gap, style="dim")
            line.append("─" * leader_width, style=border_style)
            if right:
                line.append(right_gap, style="dim")
                self._append_right(line)
            yield line
            return

        if width <= prefix_width:
            marker = Text(no_wrap=True)
            marker.append(prefix[0], style=border_style)
            if width > 1:
                marker.append(prefix[1], style="dim")
            yield marker
            for fact_line in _wrap_divider_facts(self.row.facts, width):
                yield Text(fact_line, style="dim", no_wrap=True)
            yield Text("─" * width, style=border_style, no_wrap=True)
            for status_line in wrap_display(self.row.right_status, width):
                yield Text(
                    status_line,
                    style="dim",
                    no_wrap=True,
                )
            for identity_line in wrap_display(self.row.right_identity, width):
                if identity_line:
                    yield Text(identity_line, style="dim", no_wrap=True)
            return

        indent = " " * prefix_width
        content_width = max(1, width - display_width(indent))
        fact_lines = _wrap_divider_facts(self.row.facts, content_width)
        if fact_lines:
            for index, fact_line in enumerate(fact_lines):
                line = Text(no_wrap=True)
                if index == 0:
                    line.append(prefix[0], style=border_style)
                    line.append(prefix[1:], style="dim")
                else:
                    line.append(indent, style="dim")
                line.append(fact_line, style="dim")
                yield line
        right_width = display_width(right)
        leader_width = width - display_width(indent) - right_width - 1
        if leader_width >= 3:
            line = Text(no_wrap=True)
            line.append(indent, style="dim")
            line.append("─" * leader_width, style=border_style)
            line.append(" ", style="dim")
            self._append_right(line)
            yield line
            return

        if not fact_lines:
            line = Text(no_wrap=True)
            line.append(prefix[0], style=border_style)
            line.append(prefix[1:], style="dim")
            yield line
        if right_width <= content_width:
            line = Text(indent, style="dim", no_wrap=True)
            self._append_right(line)
            yield line
            return
        for status_line in wrap_display(self.row.right_status, content_width):
            line = Text(indent, style="dim", no_wrap=True)
            line.append(
                status_line,
                style="dim",
            )
            yield line
        if self.row.right_identity:
            for identity_line in wrap_display(
                self.row.right_identity,
                content_width,
            ):
                yield Text(
                    f"{indent}{identity_line}",
                    style="dim",
                    no_wrap=True,
                )

    def _append_right(self, line: Text) -> None:
        line.append(self.row.right_status, style="dim")
        if self.row.right_identity:
            line.append(f" {self.row.right_identity}", style="dim")


def _wrap_divider_facts(facts: tuple[str, ...], width: int) -> list[str]:
    lines: list[str] = []
    for fact in facts:
        if lines and display_width(f"{lines[-1]} · {fact}") <= width:
            lines[-1] = f"{lines[-1]} · {fact}"
            continue
        wrapped = wrap_display(fact, width)
        lines.extend(wrapped or [""])
    return lines


@dataclass(frozen=True, slots=True)
class _MarkdownRow:
    row: ProgressRow
    max_width: int
    code_background: str
    code_foreground: str | None

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
            _ProgressMarkdown(
                self.row.text,
                code_theme=f"{self.code_foreground or ''}|{self.code_background}",
            ),
            options.update_width(content_width),
        )
        lines = list(Segment.split_lines(segments))
        preserve_background = True
        while lines and not _line_has_content(
            lines[0], preserve_background=preserve_background
        ):
            lines.pop(0)
        while lines and not _line_has_content(
            lines[-1], preserve_background=preserve_background
        ):
            lines.pop()
        if not lines:
            yield _plain_text(prefix.rstrip(), _STYLES[self.row.tone])
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
