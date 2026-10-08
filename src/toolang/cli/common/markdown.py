"""Shared Markdown layout and terminal-native colors for CLI output."""

from __future__ import annotations

from markdown_it.token import Token
from rich.console import Console, ConsoleOptions, RenderResult
from rich.markdown import (
    BlockQuote,
    CodeBlock,
    Heading,
    HorizontalRule,
    ListItem,
    Markdown,
    TableElement,
)
from rich.rule import Rule
from rich.segment import Segment
from rich.style import Style
from rich.syntax import Syntax, SyntaxTheme, TokenType
from rich.table import Table
from rich.text import Text
from rich.theme import Theme


class _TerminalCodeTheme(SyntaxTheme):
    """Add a default foreground while preserving the syntax theme's tokens."""

    def __init__(self, theme: SyntaxTheme, *, foreground: str) -> None:
        self._theme = theme
        self._foreground = foreground

    def get_style_for_token(self, token_type: TokenType) -> Style:
        return self._theme.get_style_for_token(token_type)

    def get_background_style(self) -> Style:
        return self._theme.get_background_style() + Style(color=self._foreground)


class _TerminalHeading(Heading):
    """Keep headings aligned with the surrounding content."""

    LEVEL_ALIGN = {
        "h1": "left",
        "h2": "left",
        "h3": "left",
        "h4": "left",
        "h5": "left",
        "h6": "left",
    }


class _TerminalHorizontalRule(HorizontalRule):
    """Render a quiet Unicode divider instead of Rich's ASCII hyphens."""

    def __rich_console__(
        self,
        console: Console,
        options: ConsoleOptions,
    ) -> RenderResult:
        del console, options
        yield Rule(style="dim", characters="─")
        yield Text()


class _TerminalCodeBlock(CodeBlock):
    """Render fenced code with the shared terminal-native palette."""

    @classmethod
    def create(cls, markdown: Markdown, token: Token) -> _TerminalCodeBlock:
        assert isinstance(markdown, TerminalMarkdown)
        lexer_name = (token.info or "").partition(" ")[0] or "text"
        return cls(
            lexer_name,
            markdown.code_theme,
            background=markdown.code_background,
            foreground=markdown.code_foreground,
        )

    def __init__(
        self,
        lexer_name: str,
        theme: str,
        *,
        background: str,
        foreground: str | None,
    ) -> None:
        super().__init__(lexer_name, theme)
        self.background = background
        self.foreground = foreground

    def __rich_console__(
        self,
        console: Console,
        options: ConsoleOptions,
    ) -> RenderResult:
        code = str(self.text).rstrip()
        theme: str | SyntaxTheme = self.theme
        if self.foreground is not None:
            theme = _TerminalCodeTheme(
                Syntax.get_theme(self.theme), foreground=self.foreground
            )
        yield Syntax(
            code,
            self.lexer_name,
            theme=theme,
            background_color=self.background,
            word_wrap=True,
            padding=(1, 2),
        )


class _TerminalTableElement(TableElement):
    """Fill the content width and fold table cells.

    Rich renders a Markdown table at its natural width and gives its columns the
    default ``overflow="ellipsis"``: a cell token wider than its column is cut
    and the remainder dropped, which silently hides message content. The
    table is adjusted after Rich builds it, so this stays tied to Rich's table
    options rather than duplicating its element assembly.
    """

    def __rich_console__(
        self,
        console: Console,
        options: ConsoleOptions,
    ) -> RenderResult:
        for renderable in super().__rich_console__(console, options):
            if isinstance(renderable, Table):
                renderable.expand = True
                renderable.show_edge = False
                for column in renderable.columns:
                    column.overflow = "fold"
            yield renderable


class _TerminalListItem(ListItem):
    """Start list markers at the content edge.

    Rich renders a marker as a three-cell field (``" • "`` for bullets, ``" 1 "``
    for numbers) and shortens the item by the same amount, which leaves every
    item one cell right of that edge and wraps its content one cell early.
    Two-cell fields give the cell back. Markers are emitted inline as segments,
    so this mirrors ``ListItem.render_bullet`` and ``ListItem.render_number``
    instead of adjusting a rendered object.
    """

    def render_bullet(
        self,
        console: Console,
        options: ConsoleOptions,
    ) -> RenderResult:
        render_options = options.update(width=options.max_width - 2)
        lines = console.render_lines(self.elements, render_options, style=self.style)
        bullet_style = console.get_style("markdown.item.bullet", default="none")
        bullet = Segment("• ", bullet_style)
        padding = Segment("  ", bullet_style)
        new_line = Segment("\n")
        for index, line in enumerate(lines):
            yield bullet if index == 0 else padding
            yield from line
            yield new_line

    def render_number(
        self,
        console: Console,
        options: ConsoleOptions,
        number: int,
        last_number: int,
    ) -> RenderResult:
        number_width = len(str(last_number)) + 1
        render_options = options.update(width=options.max_width - number_width)
        lines = console.render_lines(self.elements, render_options, style=self.style)
        number_style = console.get_style("markdown.item.number", default="none")
        new_line = Segment("\n")
        padding = Segment(" " * number_width, number_style)
        numeral = Segment(f"{number}".rjust(number_width - 1) + " ", number_style)
        for index, line in enumerate(lines):
            yield numeral if index == 0 else padding
            yield from line
            yield new_line


class _TerminalBlockQuote(BlockQuote):
    """Give quoted content the cells its two-cell bar does not use.

    Rich lays quote content out against ``max_width - 4`` while the ``"▌ "`` bar
    occupies two cells, so every quote line stops two cells short of the
    content width. Widening the options by that difference keeps the bar at the
    content edge and restores the width for the quoted text.
    """

    def __rich_console__(
        self,
        console: Console,
        options: ConsoleOptions,
    ) -> RenderResult:
        yield from super().__rich_console__(
            console,
            options.update(width=options.max_width + 2),
        )


class TerminalMarkdown(Markdown):
    """Shared terminal Markdown layout with caller-resolved code colors."""

    elements = {
        **Markdown.elements,
        "code_block": _TerminalCodeBlock,
        "fence": _TerminalCodeBlock,
        "blockquote_open": _TerminalBlockQuote,
        "heading_open": _TerminalHeading,
        "hr": _TerminalHorizontalRule,
        "list_item_open": _TerminalListItem,
        "table_open": _TerminalTableElement,
    }

    def __init__(
        self,
        markup: str,
        *,
        code_background: str,
        inline_code_background: str,
        code_foreground: str | None,
        hyperlinks: bool = True,
    ) -> None:
        super().__init__(markup, code_theme="ansi_dark", hyperlinks=hyperlinks)
        self.code_background = code_background
        self.inline_code_background = inline_code_background
        self.code_foreground = code_foreground

    def __rich_console__(
        self,
        console: Console,
        options: ConsoleOptions,
    ) -> RenderResult:
        inline_code_style = console.get_style("markdown.code", default="none") + Style(
            bgcolor=self.inline_code_background
        )
        with console.use_theme(
            Theme({"markdown.code": inline_code_style}, inherit=False)
        ):
            yield from super().__rich_console__(console, options)
