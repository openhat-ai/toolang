"""Shared CLI helpers."""

from __future__ import annotations

import os
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
import subprocess
import time
from typing import TYPE_CHECKING, Literal, cast

import click
from rich import box
from rich.console import Console
from rich.table import Table
from rich.text import Text
import typer
from typer import rich_utils as typer_rich_utils
from typer.core import TyperArgument, TyperCommand, TyperGroup

from .. import agents
from ..base.error import ToolangError
from .logo import info_avatar, info_palette

if TYPE_CHECKING:
    from ..execution.records import UpdateKind

# Typer renders command help text in dim style by default. Keep it at normal
# weight so usage notes remain easy to read in terminal help output.
setattr(typer_rich_utils, "STYLE_HELPTEXT", "")
_TABLE_CONSOLE = Console(highlight=False, width=4096)
_INFO_CONSOLE = Console(highlight=False)
TableJustify = Literal["default", "left", "center", "right", "full"]
_AGENT_AVATAR_CACHE: Text | None = None


def _agent_avatar() -> Text:
    global _AGENT_AVATAR_CACHE
    if _AGENT_AVATAR_CACHE is None:
        _AGENT_AVATAR_CACHE = info_avatar()
    return _AGENT_AVATAR_CACHE


class _PrefixAgentCommand(TyperCommand):
    """Render one virtual prefix-agent argument in help output."""

    prefix_agent_metavar = "[AGENT]"
    argument_metavar = "TEXT"
    argument_help = "Apply to this agent's caps instead of root caps."

    def _real_params(self, ctx: click.Context) -> list[click.Parameter]:
        return TyperCommand.get_params(self, ctx)

    def _prefix_agent_argument(self) -> click.Argument:
        return _HelpOnlyTyperArgument(
            param_decls=["agent"],
            metavar=self.argument_metavar,
            required=False,
            default=None,
            expose_value=False,
            help=self.argument_help,
            rich_help_panel="Scope",
        )

    def get_params(self, ctx: click.Context) -> list[click.Parameter]:
        return [self._prefix_agent_argument(), *self._real_params(ctx)]

    def format_usage(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        command_path = _strip_help_only_agent_metavars(ctx.command_path)
        root_name, _, remainder = command_path.partition(" ")
        prefix_path = (
            f"{root_name} {self.prefix_agent_metavar} {remainder}"
            if remainder
            else f"{root_name} {self.prefix_agent_metavar}"
        )
        pieces = [self.options_metavar] if self.options_metavar else []
        for param in self._real_params(ctx):
            pieces.extend(param.get_usage_pieces(ctx))
        formatter.write_usage(prefix_path, " ".join(pieces))


class _PrefixAgentWorkGroup(TyperGroup):
    """Render required AGENT between CLI root and group name in usage."""

    prefix_agent_metavar = "AGENT"

    def format_usage(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        command_path = _strip_help_only_agent_metavars(ctx.command_path)
        root_name, _, remainder = command_path.partition(" ")
        prefix_path = (
            f"{root_name} {self.prefix_agent_metavar} {remainder}"
            if remainder
            else f"{root_name} {self.prefix_agent_metavar}"
        )
        pieces = [self.options_metavar] if self.options_metavar else []
        pieces.append(self.subcommand_metavar or "[SUBCOMMAND]")
        for param in self.get_params(ctx):
            pieces.extend(param.get_usage_pieces(ctx))
        formatter.write_usage(prefix_path, " ".join(pieces))


class _OptionalPrefixAgentGroup(TyperGroup):
    """Render optional AGENT between the executable and command path in usage."""

    prefix_agent_metavar = "[AGENT]"
    argument_metavar = "TEXT"
    argument_help = "Apply to this agent's caps instead of root caps."

    def _real_params(self, ctx: click.Context) -> list[click.Parameter]:
        return TyperGroup.get_params(self, ctx)

    def _prefix_agent_argument(self) -> click.Argument:
        return _HelpOnlyTyperArgument(
            param_decls=["agent"],
            metavar=self.argument_metavar,
            required=False,
            default=None,
            expose_value=False,
            help=self.argument_help,
            rich_help_panel="Scope",
        )

    def get_params(self, ctx: click.Context) -> list[click.Parameter]:
        return [self._prefix_agent_argument(), *self._real_params(ctx)]

    def format_usage(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        command_path = _strip_help_only_agent_metavars(ctx.command_path)
        root_name, _, remainder = command_path.partition(" ")
        prefix_path = (
            f"{root_name} {self.prefix_agent_metavar} {remainder}"
            if remainder
            else f"{root_name} {self.prefix_agent_metavar}"
        )
        pieces = [self.options_metavar] if self.options_metavar else []
        pieces.append(self.subcommand_metavar or "[COMMAND] [ARGS]...")
        formatter.write_usage(prefix_path, " ".join(pieces))


class _OptionalPrefixAgentCommand(_PrefixAgentCommand):
    prefix_agent_metavar = "[AGENT]"


class _OptionalPrefixAgentListCommand(_OptionalPrefixAgentCommand):
    argument_help = "Also include this agent's caps."


class _RequiredPrefixAgentCommand(_PrefixAgentCommand):
    prefix_agent_metavar = "AGENT"
    argument_help = "Agent name."

    def _prefix_agent_argument(self) -> click.Argument:
        return _HelpOnlyTyperArgument(
            param_decls=["agent"],
            metavar=self.argument_metavar,
            required=True,
            default=None,
            expose_value=False,
            help=self.argument_help,
            rich_help_panel="Scope",
        )

    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        state = ctx.obj if isinstance(ctx.obj, dict) else {}
        if args and not args[0].startswith("-") and not state.get("agent"):
            agent = args.pop(0)
            state["agent"] = agent
            ctx.obj = state
        return TyperCommand.parse_args(self, ctx, args)


class _HelpOnlyTyperArgument(TyperArgument):
    """One help-only argument that never participates in parsing."""

    def make_metavar(self, ctx: click.Context | None = None) -> str:
        del ctx
        return self.metavar or "TEXT"

    def add_to_parser(self, parser: object, ctx: click.Context) -> None:
        del parser, ctx

    def handle_parse_result(
        self,
        ctx: click.Context,
        opts: click.core.cabc.Mapping[str, object],
        args: list[str],
    ) -> tuple[None, list[str]]:
        del ctx, opts
        return None, args


def _strip_help_only_agent_metavars(command_path: str) -> str:
    return " ".join(part for part in command_path.split() if part != "TEXT")


class _RuntimeAgentCommand(TyperCommand):
    """Render one required agent argument before the command name in help."""

    usage_agent_metavar = "AGENT"
    argument_help = "Agent name."

    def _real_params(self, ctx: click.Context) -> list[click.Parameter]:
        return TyperCommand.get_params(self, ctx)

    def _visible_real_params(self, ctx: click.Context) -> list[click.Parameter]:
        return [param for param in self._real_params(ctx) if not getattr(param, "hidden", False)]

    def _help_agent_argument(self) -> click.Argument:
        return _HelpOnlyTyperArgument(
            param_decls=["agent"],
            metavar="TEXT",
            required=True,
            default=None,
            expose_value=False,
            help=self.argument_help,
        )

    def get_params(self, ctx: click.Context) -> list[click.Parameter]:
        return [self._help_agent_argument(), *self._real_params(ctx)]

    def format_usage(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        command_path = ctx.command_path
        root_name, _, remainder = command_path.partition(" ")
        prefix_path = (
            f"{root_name} {self.usage_agent_metavar} {remainder}"
            if remainder
            else f"{root_name} {self.usage_agent_metavar}"
        )
        pieces = [self.options_metavar] if self.options_metavar else []
        for param in self._visible_real_params(ctx):
            pieces.extend(param.get_usage_pieces(ctx))
        formatter.write_usage(prefix_path, " ".join(pieces))


class _RunAgentCommand(_RuntimeAgentCommand):
    argument_help = "Existing local agent name, remote agent ref, or URL."

    def format_usage(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        pieces = [self.options_metavar] if self.options_metavar else []
        for param in self._visible_real_params(ctx):
            pieces.extend(param.get_usage_pieces(ctx))
        pieces.append(self.usage_agent_metavar)
        formatter.write_usage(ctx.command_path, " ".join(pieces))


class _StartAgentCommand(_RuntimeAgentCommand):
    argument_help = "Existing local agent name."


class _OptionalPrefixAgentTemplateCommand(_OptionalPrefixAgentCommand):
    def _help_template_argument(self) -> click.Argument:
        return _HelpOnlyTyperArgument(
            param_decls=["name"],
            metavar="TEXT",
            required=False,
            default=None,
            expose_value=False,
            help="Template name.",
        )

    def get_params(self, ctx: click.Context) -> list[click.Parameter]:
        return [
            self._prefix_agent_argument(),
            self._help_template_argument(),
            *self._real_params(ctx),
        ]


def _append_agent_update(
    toolang_root: Path,
    agent_name: str,
    update_kind: UpdateKind,
    payload: dict[str, object] | None = None,
) -> None:
    from ..execution.db import ExecutionStore, execution_db_path

    store = ExecutionStore(execution_db_path(toolang_root, agent_name))
    try:
        store.append_update(kind=update_kind, payload=payload or {})
    finally:
        store.close()


def _context_root(ctx: typer.Context) -> Path:
    state = cast(dict[str, Path | str | None], ctx.obj)
    root = state["toolang_root"]
    if not isinstance(root, Path):
        raise TypeError("missing toolang root")
    return root


def _context_agent(ctx: typer.Context) -> str | None:
    state = cast(dict[str, Path | str | None], ctx.obj)
    agent = state.get("agent")
    return agent if isinstance(agent, str) else None


def _required_prefix_agent(ctx: typer.Context, *, command_name: str) -> str:
    agent = _context_agent(ctx)
    if isinstance(agent, str) and agent:
        return agent
    del command_name
    typer.echo(ctx.get_help())
    raise typer.Exit()


def _required_runtime_agent(ctx: typer.Context, agent: str | None) -> str:
    if agent:
        return agent
    typer.echo(ctx.get_help())
    raise typer.Exit()


def _wrap_user_error(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except (FileExistsError, FileNotFoundError, ToolangError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc


def _toolang_root(explicit: Path | None) -> Path:
    if explicit is not None:
        return explicit
    return Path(os.environ.get("TOOLANG_ROOT", str(Path(os.path.expanduser("~/.toolang")))))


def _ui_base_url() -> str:
    from ..config.web import resolve_ui_base_url

    return resolve_ui_base_url(_toolang_root(None), environ=os.environ)


def _runtime_environ_for_agent(
    ctx: typer.Context,
    agent_name: str,
    *,
    toolang_root: Path | None = None,
) -> dict[str, str]:
    from ..config.env import load_runtime_environ

    root = toolang_root or _context_root(ctx)
    return load_runtime_environ(root, agent_name, base_environ=os.environ)


def _make_table(
    headers: Sequence[str],
    rows: Sequence[Sequence[str]],
    *,
    justify: Sequence[TableJustify | None] | None = None,
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


def _echo_block(text: str) -> None:
    typer.echo()
    typer.echo(text)
    typer.echo()


def _echo_table(
    headers: Sequence[str],
    rows: Sequence[Sequence[str]],
    *,
    justify: Sequence[TableJustify | None] | None = None,
) -> None:
    _TABLE_CONSOLE.print(_make_table(headers, rows, justify=justify))


def _echo_pairs_table(
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
        table.add_row(Text(key), _styled_info_value(key, value))
    typer.echo()
    if avatar is None:
        if title is None:
            _INFO_CONSOLE.print(table)
        else:
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
            _INFO_CONSOLE.print(_palette_block())
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
            right.add_row(_palette_block())
            layout.add_row(avatar_text, right)
            _INFO_CONSOLE.print(layout)
    typer.echo()


def _styled_info_value(key: str, value: str) -> Text:
    del key
    return Text(value)


def _info_title_block(title: str) -> Table:
    block = Table.grid(padding=(0, 0))
    block.add_column(no_wrap=False)
    block.add_row(Text(title, style="bold bright_cyan"))
    block.add_row(Text("-" * len(title), style="bright_black"))
    return block


def _palette_block() -> Text:
    return info_palette()


def _runtime_value(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        return "-"
    return value


def _runtime_components(runtime_state: dict[str, object]) -> str | None:
    raw = runtime_state.get("components")
    if raw is None:
        raw = runtime_state.get("features")
    if not isinstance(raw, list):
        return None
    values = [str(item).strip() for item in raw if str(item).strip()]
    if not values:
        return None
    return ", ".join(values)


def _created_time(path: Path) -> str:
    stat = path.stat()
    timestamp = getattr(stat, "st_birthtime", None)
    if timestamp is None:
        timestamp = stat.st_mtime
    return datetime.fromtimestamp(timestamp, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_utc_timestamp(value: str) -> datetime | None:
    try:
        if value.endswith("Z"):
            return datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _format_runtime_row(status: agents.AgentStatus) -> str:
    return f"{status.name}\t{status.status}\t{status.api_url or '-'}\t{status.webui_url or '-'}"


def _normalize_component_option(components: list[str] | None) -> list[str] | None:
    if components is None:
        return None
    normalized: list[str] = []
    for item in components:
        for value in item.split(","):
            component_name = value.strip()
            if component_name:
                normalized.append(component_name)
    return normalized


def _wait_for_started_status(
    *,
    root: Path,
    agent_name: str,
    process: subprocess.Popen[bytes],
    launched_at: float,
    timeout_sec: float,
) -> agents.AgentStatus | None:
    deadline = time.monotonic() + timeout_sec
    state_path = agents.agent_runtime_state_path(root, agent_name)
    while time.monotonic() < deadline:
        if state_path.is_file() and state_path.stat().st_mtime >= launched_at - 0.01:
            status = agents.get_agent_status(root, agent_name, ui_base_url=_ui_base_url())
            if status is not None and status.status in {"running", "failed"}:
                return status
        if process.poll() is not None:
            break
        time.sleep(0.1)
    if state_path.is_file() and state_path.stat().st_mtime >= launched_at - 0.01:
        return agents.get_agent_status(root, agent_name, ui_base_url=_ui_base_url())
    return None
