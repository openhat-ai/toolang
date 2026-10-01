"""Shared Rich output for Toolang command-line interfaces."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
import os
from pathlib import Path
import tempfile
from typing import TYPE_CHECKING, Literal, cast

from rich import box
from rich.console import Console
from rich.padding import Padding
from rich.table import Table
from rich.text import Text
import typer
from typer._click.exceptions import (
    ClickException,
    MissingParameter,
    NoArgsIsHelpError,
    UsageError,
)

from toolang.common.typer.ui import UV, HelpFormatter

if TYPE_CHECKING:
    from toolang.up.process import AgentStatus

TableJustify = Literal["default", "left", "center", "right", "full"]
TableCell = str | Text

_TABLE_CONSOLE = Console(highlight=False, width=4096)
_INFO_CONSOLE = Console(highlight=False)

TOOLANG_LOGO_TEXT = """
████        ██
 ██  ⬤  ⬤   ██
 ██        ███
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


def echo_error(error: str | ClickException) -> None:
    """Render errors with the CLI formatter while keeping business tables separate."""
    console = Console(stderr=True, highlight=False, theme=UV)
    console.width = min(console.width, 120)
    formatter = HelpFormatter(console=console)
    with console.use_theme(UV):
        if (
            isinstance(error, (MissingParameter, NoArgsIsHelpError))
            and error.ctx is not None
        ):
            formatter.write_help(error.ctx)
        else:
            message = (
                error.format_message() if isinstance(error, ClickException) else error
            )
            formatter.write_error(
                message, error.ctx if isinstance(error, UsageError) else None
            )
    formatter.console.print(Text.from_ansi(formatter.getvalue()), soft_wrap=True)


def echo_table(
    headers: Sequence[str],
    rows: Sequence[Sequence[TableCell]],
    *,
    justify: Sequence[TableJustify | None] | None = None,
    max_widths: Sequence[int | None] | None = None,
) -> None:
    _TABLE_CONSOLE.print(
        _make_table(headers, rows, justify=justify, max_widths=max_widths)
    )


def echo_collection_summary(
    count: int, noun: str, *, group: tuple[int, str] | None = None
) -> None:
    """Summarize displayed rows, including empty and singleton collections."""

    plural = noun + ("es" if noun.endswith(("s", "x", "ch", "sh")) else "s")
    summary = f"{count} {noun if count == 1 else plural}"
    if count > 1 and group is not None:
        groups, group_noun = group
        summary += f", {groups} {group_noun if groups == 1 else group_noun + 's'}"
    if count:
        typer.echo()
    typer.echo(summary)


def echo_pairs_table(
    rows: Sequence[tuple[str, str]],
    *,
    avatar: Text | str | None = None,
    title: str | None = None,
) -> None:
    table = Table(
        box=None,
        padding=(0, 2),
        header_style="",
        show_header=False,
        show_lines=False,
        pad_edge=False,
        collapse_padding=True,
    )
    table.add_column("INFO", no_wrap=False, overflow="fold")
    for key, value in rows:
        table.add_row(Text.assemble((f"{key}:", "bold yellow"), f" {value}"))
    palette = _info_palette()
    if avatar is None:
        typer.echo()
        if title is not None:
            _INFO_CONSOLE.print(_info_title_block(title))
        _INFO_CONSOLE.print(table)
        if palette is not None:
            typer.echo()
            _INFO_CONSOLE.print(palette)
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
        if palette is not None:
            layout.add_row(Text(""), palette)
            layout.add_row(Text(""), Text(""))
        _INFO_CONSOLE.print(Padding(layout, (0, 0, 0, 3), expand=False))


def _info_palette() -> Text | None:
    """Show the terminal's standard and bright ANSI backgrounds in two rows."""

    if _INFO_CONSOLE.color_system is None or _INFO_CONSOLE.no_color:
        return None
    palette = Text(no_wrap=True, overflow="crop")
    for index in range(16):
        if index == 8:
            palette.append("\n")
        palette.append("   ", style=f"on color({index})")
    return palette


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


def runnable_label(kind: str | None, name: str | None) -> str:
    """Return one compact runnable label for CLI output."""

    normalized_kind = (kind or "run").strip() or "run"
    normalized_name = (name or "").strip()
    return (
        f"{normalized_kind}:{normalized_name}" if normalized_name else normalized_kind
    )


def active_agent_error(status: AgentStatus) -> str:
    message = f"Agent {status.name} already {status.status}"
    detail = (
        (status.webui_url or status.api_url) if status.status == "running" else None
    )
    return f"{message}: {detail}" if detail else message


def _make_table(
    headers: Sequence[str],
    rows: Sequence[Sequence[TableCell]],
    *,
    justify: Sequence[TableJustify | None] | None,
    max_widths: Sequence[int | None] | None = None,
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
        max_width = max_widths[index] if max_widths is not None else None
        table.add_column(
            header,
            no_wrap=max_width is None,
            justify=column_justify,
            max_width=max_width,
            overflow="fold" if max_width is not None else "ellipsis",
        )
    for row in rows:
        table.add_row(*(_table_cell_text(cell) for cell in row))
    return table


def _table_cell_text(cell: TableCell) -> Text:
    if isinstance(cell, Text):
        return cell
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
    block.add_row(Text(title, style="bold green"))
    block.add_row(Text("─" * len(title), style="bright_black"))
    return block
