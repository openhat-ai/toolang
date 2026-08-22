"""Shared Rich output for Toolang command-line interfaces."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
import os
from pathlib import Path
import tempfile
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

TOOLANG_LOGO_TEXT = """
████           ██
 ██   ⬤   ⬤    ██
 ██          ████
""".strip("\n")
TOOLANG_COLOR = "bright_cyan"


def toolang_logo_text() -> str:
    """Return the plain compact Toolang logo."""

    return TOOLANG_LOGO_TEXT


def toolang_logo(console: Console) -> Text:
    """Return the compact Toolang logo for one terminal console."""

    if console.color_system is None or console.no_color:
        return Text(TOOLANG_LOGO_TEXT)

    logo = Text()
    for character in TOOLANG_LOGO_TEXT:
        if character == "█":
            style = f"{TOOLANG_COLOR} on {TOOLANG_COLOR}"
        elif character == "⬤":
            style = TOOLANG_COLOR
        else:
            style = None
        logo.append(character, style=style)
    return logo


def info_avatar_text() -> str:
    """Return the plain CLI info avatar art."""

    return toolang_logo_text()


def agent_avatar() -> Text:
    """Return the avatar used by agent information views."""

    return toolang_logo(_INFO_CONSOLE)


def shorten_home_path(path: Path) -> str:
    """Return a compact, platform-native label for one agent home path."""

    resolved = path.expanduser().resolve(strict=False)
    temporary_roots: list[tuple[Path, str]] = []
    if os.name != "nt":
        temporary_roots.append((Path("/tmp").resolve(strict=False), "/tmp"))
    native_temp = Path(tempfile.gettempdir())
    native_temp_resolved = native_temp.resolve(strict=False)
    native_temp_label = str(native_temp)
    environment_names = ("TEMP", "TMP") if os.name == "nt" else ("TMPDIR",)
    for name in environment_names:
        value = os.environ.get(name)
        if value and Path(value).resolve(strict=False) == native_temp_resolved:
            native_temp_label = f"%{name}%" if os.name == "nt" else f"${name}"
            break
    temporary_roots.append((native_temp_resolved, native_temp_label))
    for root, label in temporary_roots:
        if resolved.is_relative_to(root):
            relative = resolved.relative_to(root)
            return label if not relative.parts else str(Path(label) / relative)

    user_home = Path.home().resolve(strict=False)
    if resolved.is_relative_to(user_home):
        relative = resolved.relative_to(user_home)
        return "~" if not relative.parts else str(Path("~") / relative)
    return str(resolved)


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
    table.add_column("VALUE", no_wrap=False, overflow="fold")
    for key, value in rows:
        table.add_row(Text(key), Text(value))
    if avatar is None:
        typer.echo()
        if title is not None:
            _INFO_CONSOLE.print(_info_title_block(title))
        _INFO_CONSOLE.print(table)
        typer.echo()
    else:
        avatar_text = avatar if isinstance(avatar, Text) else Text(avatar)
        layout = Table.grid(padding=(0, 4))
        layout.add_column(no_wrap=True, ratio=0, vertical="top")
        layout.add_column(no_wrap=False, ratio=1, vertical="top")
        layout.add_row(Text(""), Text(""))
        if title is not None:
            layout.add_row(Text(""), _info_title_block(title))
        layout.add_row(avatar_text, table)
        layout.add_row(Text(""), Text(""))
        _INFO_CONSOLE.print(Padding(layout, (0, 0, 0, 3), expand=False))


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
        f"{normalized_kind}:{normalized_name}" if normalized_name else normalized_kind
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
    block.add_row(Text("─" * len(title), style="bright_black"))
    return block
