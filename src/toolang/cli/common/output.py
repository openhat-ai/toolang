"""Shared Rich output for Toolang command-line interfaces."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

import click
from rich import box
from rich.console import Console
from rich.padding import Padding
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
import typer
from typer import rich_utils

from toolang.up import process as agents

TableJustify = Literal["default", "left", "center", "right", "full"]

_TABLE_CONSOLE = Console(highlight=False, width=4096)
_INFO_CONSOLE = Console(highlight=False)
_AGENT_AVATAR_CACHE: Text | None = None

INFO_AVATAR_TEXT = """
██████████                    ████
▀▀▀████▀▀▀                    ████
   ████     ▄▄▄▄     ▄▄▄▄     ████
   ████     ████     ████     ████
   ████     ▀▀▀▀     ▀▀▀▀     ████
   ████                   ▄▄▄▄████
   ████                   ████████
""".strip("\n")

INFO_AVATAR_MARGIN = " "

INFO_PALETTE_COLORS = (
    (
        "63c74d",
        "9edb49",
        "f4d35e",
        "f39c12",
        "f45b69",
        "ff66c4",
        "c77dff",
        "8e7dff",
        "5b8cff",
        "4cc9f0",
        "43d9bd",
    ),
    (
        "4fb03d",
        "89c93b",
        "e6c24f",
        "de8a0d",
        "de4a58",
        "f055b5",
        "b56aed",
        "7b6cf0",
        "4b79ee",
        "3cb7de",
        "36c4aa",
    ),
)


def info_avatar() -> Text:
    """Return the styled CLI info avatar art."""

    return _rainbow_text(info_avatar_text())


def info_avatar_text() -> str:
    """Return the plain CLI info avatar art."""

    return _with_line_margin(INFO_AVATAR_TEXT, margin=INFO_AVATAR_MARGIN)


def info_palette() -> Text:
    """Return the styled two-row CLI info palette."""

    top, bottom = INFO_PALETTE_COLORS
    palette = Text()
    for style in _color_row_styles(top):
        palette.append("██", style=style)
    palette.append("\n")
    for style in _color_row_styles(bottom):
        palette.append("██", style=style)
    return palette


def agent_avatar() -> Text:
    """Return the cached avatar used by agent information views."""

    global _AGENT_AVATAR_CACHE
    if _AGENT_AVATAR_CACHE is None:
        _AGENT_AVATAR_CACHE = info_avatar()
    return _AGENT_AVATAR_CACHE


def echo_block(text: str) -> None:
    typer.echo()
    typer.echo(text)
    typer.echo()


def echo_error(error: str | click.ClickException) -> None:
    """Render one terminal error with the shared Typer Rich presentation."""

    exception = (
        error
        if isinstance(error, click.ClickException)
        else click.ClickException(error)
    )
    console = rich_utils._get_rich_console(stderr=True)
    ctx = getattr(exception, "ctx", None)
    if isinstance(ctx, click.Context):
        console.print(
            Padding(rich_utils.highlighter(ctx.get_usage()), 1),
            style=rich_utils.STYLE_USAGE_COMMAND,
        )
        if ctx.command.get_help_option(ctx) is not None:
            console.print(
                Padding(
                    rich_utils.RICH_HELP.format(
                        command_path=ctx.command_path,
                        help_option=ctx.help_option_names[0],
                    ),
                    (0, 1, 1, 1),
                ),
                style=rich_utils.STYLE_ERRORS_SUGGESTION,
            )
    else:
        console.print()
    console.print(
        Panel(
            rich_utils.highlighter(exception.format_message()),
            border_style=rich_utils.STYLE_ERRORS_PANEL_BORDER,
            title=rich_utils.ERRORS_PANEL_TITLE,
            title_align=rich_utils.ALIGN_ERRORS_PANEL,
        )
    )
    console.print()


def echo_table(
    headers: Sequence[str],
    rows: Sequence[Sequence[str]],
    *,
    justify: Sequence[TableJustify | None] | None = None,
) -> None:
    _TABLE_CONSOLE.print(_make_table(headers, rows, justify=justify))


def echo_pairs_table(
    rows: Sequence[tuple[str, str]],
    *,
    avatar: Text | str | None = None,
    title: str | None = None,
) -> None:
    table = Table(
        box=None,
        header_style="",
        show_header=False,
        show_lines=False,
        pad_edge=False,
        collapse_padding=True,
    )
    table.add_column("FIELD", no_wrap=True, style="bold bright_cyan")
    table.add_column("VALUE", no_wrap=False, style="white", overflow="fold")
    for key, value in rows:
        table.add_row(Text(key), Text(value))
    typer.echo()
    if avatar is None:
        if title is not None:
            _INFO_CONSOLE.print(_info_title_block(title))
        _INFO_CONSOLE.print(table)
    else:
        avatar_text = avatar if isinstance(avatar, Text) else Text(avatar)
        if _INFO_CONSOLE.width < 100:
            _INFO_CONSOLE.print(avatar_text)
            _INFO_CONSOLE.print(Text(""))
            if title is not None:
                _INFO_CONSOLE.print(_info_title_block(title))
            _INFO_CONSOLE.print(table)
            _INFO_CONSOLE.print(Text(""))
            _INFO_CONSOLE.print(info_palette())
        else:
            layout = Table.grid(padding=(0, 4))
            layout.add_column(no_wrap=True, ratio=0)
            layout.add_column(no_wrap=False, ratio=1)
            right = Table.grid(padding=(0, 0))
            right.add_column(no_wrap=False)
            if title is not None:
                layout.add_row(Text(""), _info_title_block(title))
            right.add_row(table)
            right.add_row(Text(""))
            right.add_row(info_palette())
            layout.add_row(avatar_text, right)
            _INFO_CONSOLE.print(layout)
    typer.echo()


def created_time(path: Path) -> str:
    stat = path.stat()
    timestamp = getattr(stat, "st_birthtime", None)
    if timestamp is None:
        timestamp = stat.st_mtime
    return datetime.fromtimestamp(timestamp, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_utc_timestamp(value: str) -> datetime | None:
    try:
        if value.endswith("Z"):
            return datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def runtime_value(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        return "-"
    return value


def executable_label(kind: str | None, name: str | None) -> str:
    """Return one compact executable label for CLI output."""

    normalized_kind = (kind or "run").strip() or "run"
    normalized_name = (name or "").strip()
    return (
        f"{normalized_kind}:{normalized_name}"
        if normalized_name
        else normalized_kind
    )


def active_agent_error(status: agents.AgentStatus) -> str:
    message = f"Agent {status.name} already {status.status}"
    detail = (
        (status.webui_url or status.api_url) if status.status == "running" else None
    )
    return f"{message}: {detail}" if detail else message


def _make_table(
    headers: Sequence[str],
    rows: Sequence[Sequence[str]],
    *,
    justify: Sequence[TableJustify | None] | None,
) -> Table:
    table = Table(
        box=box.HORIZONTALS,
        header_style="",
        show_lines=False,
        pad_edge=False,
        collapse_padding=True,
    )
    for index, header in enumerate(headers):
        column_justify: TableJustify = "left"
        if justify is not None and index < len(justify) and justify[index] is not None:
            column_justify = cast(TableJustify, justify[index])
        table.add_column(header, no_wrap=True, justify=column_justify)
    for row in rows:
        table.add_row(*(_table_cell_text(cell) for cell in row))
    return table


def _table_cell_text(cell: str) -> Text:
    text = Text(cell)
    for marker, style in (
        ("(missing)", "bold red"),
        ("(offline)", "bold yellow"),
        ("(auth failed)", "bold red"),
        ("(error)", "bold red"),
    ):
        start = 0
        while True:
            index = cell.find(marker, start)
            if index < 0:
                break
            text.stylize(style, index, index + len(marker))
            start = index + len(marker)
    return text


def _info_title_block(title: str) -> Table:
    block = Table.grid(padding=(0, 0))
    block.add_column(no_wrap=False)
    block.add_row(Text(title, style="bold bright_cyan"))
    block.add_row(Text("-" * len(title), style="bright_black"))
    return block


def _rainbow_text(text: str) -> Text:
    rainbow_styles = _color_row_styles(INFO_PALETTE_COLORS[0])
    styled = Text()
    for row, line in enumerate(text.splitlines()):
        for column, char in enumerate(line):
            if char == " ":
                styled.append(char)
                continue
            style_index = (column + (row * 2)) % len(rainbow_styles)
            styled.append(char, style=rainbow_styles[style_index])
        styled.append("\n")
    if styled.plain.endswith("\n"):
        styled = styled[:-1]
    return styled


def _with_line_margin(text: str, *, margin: str) -> str:
    return "\n".join(f"{margin}{line}{margin}" for line in text.splitlines())


def _color_row_styles(colors: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(f"bold #{color}" for color in colors)
