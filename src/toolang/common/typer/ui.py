"""A uv-style help formatter, Rich themes, and a Typer entry point."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from inspect import cleandoc
from itertools import zip_longest
from typing import Any

import typer
from rich.cells import cell_len
from rich.console import COLOR_SYSTEMS, Console
from rich.text import Text
from rich.theme import Theme
from typer._click.core import Command, Context, augment_usage_errors
from typer._click.exceptions import (
    ClickException,
    MissingParameter,
    NoArgsIsHelpError,
    UsageError,
)
from typer._click.formatting import HelpFormatter as TyperHelpFormatter
from typer._click.types import FloatRange, IntRange
from typer._types import TyperChoice
from typer.core import TyperArgument, TyperCommand, TyperGroup, TyperOption
from typer.main import get_command

_PREPARE = f"{__name__}.prepare"

PLAIN = Theme(
    {
        "cli.heading": "bold",
        "cli.usage": "",
        "cli.argument.name": "bold",
        "cli.option.name": "bold",
        "cli.option.metavar": "dim",
        "cli.command.name": "bold",
        "cli.required": "",
        "cli.meta": "dim",
        "cli.error": "",
    }
)

UV = Theme(
    {
        **PLAIN.styles,
        "cli.heading": "bold green",
        "cli.usage": "cyan",
        "cli.argument.name": "bold cyan",
        "cli.option.name": "bold cyan",
        "cli.option.metavar": "cyan",
        "cli.command.name": "bold cyan",
        "cli.required": "red",
        "cli.error": "red",
    }
)


def run(
    app: typer.Typer,
    *,
    theme: Theme = UV,
    args: Sequence[str] | None = None,
    prog_name: str | None = None,
    console: Console | None = None,
    error_console: Console | None = None,
    debug: bool = False,
    **context_settings: Any,
) -> int:
    """Run an existing Typer app with uv help and the UV theme by default.

    Command format_help hooks and HelpFormatter subclasses are preserved.
    Consoles control help and errors; command output remains with the application.
    """
    if not isinstance(theme, Theme):
        raise TypeError("theme must be a Rich Theme")
    console = console if console is not None else _default_console()
    error_console = (
        error_console if error_console is not None else _default_console(stderr=True)
    )

    def show_help(ctx: Context, *, error: bool = False) -> None:
        output = error_console if error else console
        output.print(
            Text.from_ansi(_format_help(ctx, theme=theme, console=output)),
            soft_wrap=True,
        )

    def show_error(message: str, ctx: Context | None = None) -> None:
        with error_console.use_theme(theme):
            formatter = _make_formatter(ctx, error_console)
            formatter.write_error(message, ctx)
        error_console.print(Text.from_ansi(formatter.getvalue()), soft_wrap=True)

    command = _prepare(
        get_command(app),
        lambda ctx: _format_help(ctx, theme=theme, console=console),
        show_help,
    )
    native_invoke = command.invoke

    def invoke(ctx: Context) -> None:
        # Standalone Typer ignores callback results; explicit Exit codes still propagate.
        native_invoke(ctx)

    command.invoke: Callable[[Context], None] = invoke
    try:
        result = command.main(
            args=args, prog_name=prog_name, standalone_mode=False, **context_settings
        )
        return result if isinstance(result, int) else 0
    except ClickException as exc:
        if (
            isinstance(exc, (MissingParameter, NoArgsIsHelpError))
            and exc.ctx is not None
        ):
            show_help(exc.ctx, error=True)
        else:
            show_error(
                exc.format_message(), exc.ctx if isinstance(exc, UsageError) else None
            )
        return exc.exit_code
    except typer.Abort:
        show_error("Aborted.")
        return 1
    except Exception as exc:
        show_error(str(exc) or type(exc).__name__)
        if debug:
            with error_console.use_theme(theme):
                error_console.print()
                error_console.print_exception(show_locals=False)
        return 1


def _format_help(ctx: Context, *, theme: Theme, console: Console | None = None) -> str:
    console = console if console is not None else _default_console()
    with console.use_theme(theme):
        formatter = _make_formatter(ctx, console)
        if type(ctx.command).format_help in (
            Command.format_help,
            TyperCommand.format_help,
            TyperGroup.format_help,
        ):
            formatter.write_help(ctx)
        else:
            ctx.command.format_help(ctx, formatter)
        return formatter.getvalue().rstrip("\n")


def _make_formatter(ctx: Context | None, console: Console) -> HelpFormatter:
    formatter_class = HelpFormatter
    if ctx is not None and issubclass(ctx.formatter_class, HelpFormatter):
        formatter_class = ctx.formatter_class
    return formatter_class(
        width=ctx.terminal_width if ctx is not None else None,
        max_width=ctx.max_content_width if ctx is not None else None,
        console=console,
    )


def inherit_ui(command: Command, parent: Context | None) -> Command:
    """Apply a parent's UI to a command loaded on demand, before parsing it."""
    if parent is not None and (prepare := parent.meta.get(_PREPARE)) is not None:
        return prepare(command)
    return command


class HelpFormatter(TyperHelpFormatter):
    """Native help sections and indentation, with styled definition lists."""

    def __init__(
        self,
        indent_increment: int = 2,
        width: int | None = None,
        max_width: int | None = None,
        *,
        console: Console | None = None,
        column_gap: int = 1,
        description_gap: int = 2,
    ):
        if min(indent_increment, column_gap, description_gap) < 0:
            raise ValueError("Layout spacing must be nonnegative.")
        if console is None:
            console = _default_console()
        width = width or console.width
        super().__init__(
            indent_increment=indent_increment, width=min(width, max_width or width)
        )
        self.column_gap = column_gap
        self.description_gap = description_gap
        self.console = console

    def write_help(self, ctx: Context) -> None:
        command = ctx.command
        self.write_description(ctx)
        self.write_usage(ctx)
        params = command.get_params(ctx)
        self._sections(
            (
                (param.rich_help_panel, self._argument_row(param, ctx))
                for param in params
                if isinstance(param, TyperArgument) and not param.hidden
            ),
            "Arguments",
        )
        self.write_commands(ctx)
        self._sections(
            (
                (param.rich_help_panel, self._option_row(param, ctx))
                for param in params
                if isinstance(param, TyperOption) and not param.hidden
            ),
            "Options",
            shared_columns=False,
        )
        self.write_epilog(ctx)

    def write_description(self, ctx: Context) -> None:
        if description := _command_description(ctx.command, short=False):
            if not description.endswith("."):
                description += "."
            self.write_text(description)
            self.write_paragraph()

    def write_commands(self, ctx: Context) -> None:
        """Render a group's command directory without its Usage or options."""
        self._sections(self._command_rows(ctx), "Commands")

    def write_epilog(self, ctx: Context) -> None:
        if epilog := cleandoc(ctx.command.epilog or "").rstrip():
            self.write_paragraph()
            self.write_text(epilog)

    def write_error(self, message: str, ctx: Context | None) -> None:
        self.write_text(Text.assemble(("Error: ", "cli.error"), message))
        if ctx is not None:
            self.write_paragraph()
            self.write_usage(ctx)

    def write_heading(self, heading: str) -> None:
        self.write_text(Text(heading + ":", style="cli.heading"))

    def write_text(self, text: str | Text) -> None:
        text = text if isinstance(text, Text) else Text(text)
        for line in self._lines(text, max(1, self.width - self.current_indent)):
            self._write_line(line)

    def write_usage(
        self, prog: str | Text | Context, args: str = "", prefix: str | None = None
    ) -> None:
        if isinstance(prog, Context):
            if type(prog.command).format_usage is not Command.format_usage:
                prog.command.format_usage(prog, self)
                return
            text = _usage(prog)
        else:
            text = Text.assemble(
                ("Usage: " if prefix is None else prefix, "cli.heading"),
                prog
                if isinstance(prog, Text)
                else Text(prog, style="cli.command.name"),
                (" " + args if args else "", "cli.usage"),
            )
        self.write_text(text)

    def write_dl(
        self,
        rows: Sequence[tuple[str | Text, ...]],
        col_max: int = 30,
        col_spacing: int = 2,
    ) -> None:
        """Write a definition list with an optional marker/short-alias column."""
        if any(len(row) not in (2, 3) for row in rows):
            raise ValueError("Definition lists require two or three columns.")
        rows = [
            tuple(value if isinstance(value, Text) else Text(value) for value in row)
            for row in rows
        ]
        rows = [(Text(), *row) if len(row) == 2 else row for row in rows]
        if not rows:
            return
        available = max(2, self.width - self.current_indent)
        prefix_width = min(_width(row[0] for row in rows), max(1, available // 4))
        prefix_gap = self.column_gap if prefix_width else 0
        body_width = max(2, available - prefix_width - prefix_gap - col_spacing)
        label_width = min(
            max(1, _width(row[1] for row in rows)), col_max, max(1, body_width // 2)
        )
        description_width = body_width - label_width

        for prefix, label, description in rows:
            columns = (
                self._lines(prefix, prefix_width),
                self._lines(label, label_width),
                self._lines(description, description_width),
            )
            for first, second, third in zip_longest(*columns, fillvalue=Text()):
                line = Text.assemble(first)
                line.append(" " * (prefix_width - first.cell_len + prefix_gap))
                line.append(second)
                line.append(" " * (label_width - second.cell_len + col_spacing))
                line.append(third)
                line.rstrip()
                self._write_line(line)

    def _sections(self, entries, default: str, *, shared_columns: bool = True) -> None:
        groups: dict[str, list[tuple[Text, Text, Text]]] = {}
        for title, row in entries:
            groups.setdefault(title or default, []).append(row)
        rows = [row for group in groups.values() for row in group]
        prefix_width = _width(row[0] for row in rows)
        label_width = _width(row[1] for row in rows) if shared_columns else 0
        for title, group in groups.items():
            aligned = []
            for prefix, label, description in group:
                prefix, label = prefix.copy(), label.copy()
                prefix.pad_right(max(0, prefix_width - prefix.cell_len))
                label.pad_right(max(0, label_width - label.cell_len))
                aligned.append((prefix, label, description))
            with self.section(title):
                self.write_dl(
                    aligned, col_max=self.width, col_spacing=self.description_gap
                )

    def _lines(self, text: Text, width: int):
        if not width:
            return [Text()]
        text = text.copy()
        text.rstrip()
        lines = text.wrap(self.console, width, justify="left", overflow="fold")
        for line in lines:
            line.rstrip()
        return lines or [Text()]

    def _write_line(self, text: Text) -> None:
        line = Text.assemble(" " * self.current_indent, text)
        color_system = COLOR_SYSTEMS.get(self.console.color_system or "")
        for content, style, _ in line.render(self.console, end="\n"):
            if style and self.console.no_color:
                style = style.without_color
            self.write(
                style.render(content, color_system=color_system) if style else content
            )

    def _argument_row(
        self, param: TyperArgument, ctx: Context
    ) -> tuple[Text, Text, Text]:
        marker = Text("*" if param.required else "", style="cli.required")
        label = Text(param.metavar or param.name or "", style="cli.argument.name")
        return marker, label, _parameter_help(param, ctx)

    def _option_row(self, param: TyperOption, ctx: Context) -> tuple[Text, Text, Text]:
        short_groups, long_groups = [], []
        for aliases in (param.opts, param.secondary_opts):
            short = [
                name for name in aliases if name.startswith("-") and len(name) == 2
            ]
            long = [name for name in aliases if name not in short]
            if short:
                short_groups.append(", ".join(short))
            if long:
                long_groups.append(", ".join(long))
        short, long = " / ".join(short_groups), " / ".join(long_groups)
        aliases = Text(short + ("," if short and long else ""), style="cli.option.name")
        label = Text()
        label.append(long, style="cli.option.name")
        if (metavar := _value_label(param, ctx)) is not None:
            if long:
                label.append(" ")
            label.append(metavar, style="cli.option.metavar")
        return aliases, label, _parameter_help(param, ctx)

    def _command_rows(self, ctx: Context):
        if isinstance(ctx.command, TyperGroup):
            for name in ctx.command.list_commands(ctx):
                command = ctx.command.get_command(ctx, name)
                if command is not None and not command.hidden:
                    yield (
                        getattr(command, "rich_help_panel", None),
                        (
                            Text(),
                            Text(name, style="cli.command.name"),
                            Text(
                                _command_description(command),
                                justify="left",
                                overflow="fold",
                            ),
                        ),
                    )


def _public_help(value: str | None) -> str:
    return cleandoc(value or "").split("\f", 1)[0].strip()


def _command_description(command: Command, *, short: bool = True) -> str:
    description = _public_help((command.short_help if short else None) or command.help)
    if short and not command.short_help:
        description = description.split("\n\n", 1)[0]
    if command.deprecated:
        note = (
            f"DEPRECATED: {command.deprecated}"
            if isinstance(command.deprecated, str)
            else "DEPRECATED"
        )
        description = f"{description} ({note})".strip()
    return description


def _usage(ctx: Context) -> Text:
    contexts = []
    current: Context | None = ctx
    while current is not None:
        contexts.append(current)
        current = current.parent

    text = Text(style="cli.usage")
    text.append("Usage:", style="cli.heading")
    for current in reversed(contexts):
        command = current.command
        if current.info_name:
            text.append(" ")
            text.append(current.info_name, style="cli.command.name")
        if current is ctx and command.options_metavar:
            text.append(" " + command.options_metavar)
        for param in command.get_params(current):
            if isinstance(param, TyperArgument):
                name = (param.metavar or param.name or "").upper()
                if param.nargs == -1:
                    name += "..."
                name = " ".join([name] * max(1, param.nargs))
                text.append(" ")
                text.append(name if param.required else f"[{name}]")
            else:
                for part in param.get_usage_pieces(current):
                    text.append(" " + part)
        if current is ctx and isinstance(command, TyperGroup):
            text.append(" " + command.subcommand_metavar)
    return text


def _parameter_help(param: TyperArgument | TyperOption, ctx: Context) -> Text:
    description = param.help or ""
    if isinstance(param, TyperOption) and "--help" in param.opts:
        description = description.rstrip().removesuffix(".")
    text = Text(description, justify="left", overflow="fold")
    fields = []
    if param.show_envvar:
        declared = param.envvar
        if (
            declared is None
            and isinstance(param, TyperOption)
            and param.allow_from_autoenv
            and ctx.auto_envvar_prefix is not None
            and param.name is not None
        ):
            declared = f"{ctx.auto_envvar_prefix}_{param.name.upper()}"
        if declared is not None:
            envvars = (declared,) if isinstance(declared, str) else tuple(declared)
            if envvars:
                fields.append("env: " + ", ".join(f"{name}=" for name in envvars))

    # Typer exposes these same helpers for its own Rich renderer. They preserve
    # default_map, callable, enum, and boolean-flag semantics without invoking
    # a default factory or interpreting formatted help text.
    value = param._extract_default_help_str(ctx=ctx)
    named_default = isinstance(param.show_default, str)
    if named_default or (
        value is not None and (param.show_default or ctx.show_default)
    ):
        default = param._get_default_string(
            ctx=ctx, show_default_is_str=named_default, default_value=value
        )
        if default:
            fields.append(f"default: {default}")

    # Optional-value extensions may provide declarative display values without
    # coupling this standalone formatter to their parser implementation.
    get_bare_help = getattr(ctx.command, "get_bare_help", None)
    if (
        isinstance(param, TyperOption)
        and param.name is not None
        and callable(get_bare_help)
    ):
        if (bare := get_bare_help(param.name)) is not None:
            fields.append(f"bare: {bare}")

    if isinstance(param, TyperOption) and isinstance(
        param.type, (IntRange, FloatRange)
    ):
        if range_description := param.type._describe_range():
            fields.append(range_description)
    if isinstance(param, TyperOption) and param.required:
        fields.append("required")
    if fields:
        if text:
            text.append("  ")
        text.append(" ".join(f"[{field}]" for field in fields), style="cli.meta")
    return text


def _value_label(param: TyperOption, ctx: Context) -> str | None:
    if param.is_flag or param.count:
        return None
    label = param.make_metavar(ctx)
    if param.metavar is None and (
        param.type.get_metavar(param=param, ctx=ctx) is None
        or isinstance(param.type, TyperChoice)
        and not param.show_choices
    ):
        # Only generated type names are uppercased, not literal choices or formats.
        label = label.upper()
    value = label.removesuffix("...")
    # Declarations may bracket individual values, as in key=<VALUE> or [PATH].
    if ("[" in value and value.endswith("]")) or ("<" in value and value.endswith(">")):
        return label
    return f"<{value}>{label[len(value) :]}"


def _width(values: Iterable[Text]) -> int:
    return max(
        (cell_len(line) for value in values for line in value.plain.splitlines()),
        default=0,
    )


def _default_console(*, stderr: bool = False) -> Console:
    console = Console(stderr=stderr, highlight=False, theme=UV)
    console.width = min(console.width, 120)
    return console


def _prepare(
    command: Command,
    render_help: Callable[[Context], str],
    show_help: Callable[[Context], None],
) -> Command:
    """Adapt help and parsing on this run's command tree, preserving native classes.

    Typer's Rich path bypasses HelpFormatter and has no per-app renderer hook.
    The help callback prints once; get_help keeps the string-returning API.
    """
    native_parse = command.parse_args
    native_help_option = command.get_help_option

    def help_option(ctx: Context, param: TyperOption | None, value: bool) -> None:
        if value and not ctx.resilient_parsing:
            show_help(ctx)
            ctx.exit()

    def get_help_option(ctx: Context) -> TyperOption | None:
        option = native_help_option(ctx)
        if option is not None:
            option.callback = help_option
        return option

    def parse_args(ctx: Context, args: list[str]) -> list[str]:
        ctx.meta[_PREPARE] = lambda child: _prepare(child, render_help, show_help)
        empty_group_help = (
            isinstance(command, TyperGroup)
            and not command.invoke_without_command
            and not any(param.required for param in command.params)
        )
        if not args and (command.no_args_is_help or empty_group_help):
            help_option(ctx, None, True)
        with augment_usage_errors(ctx):
            return native_parse(ctx, args)

    command.get_help: Callable[[Context], str] = render_help
    command.get_help_option: Callable[[Context], TyperOption | None] = get_help_option
    command.parse_args: Callable[[Context, list[str]], list[str]] = parse_args
    if isinstance(command, TyperGroup):
        for child in command.commands.values():
            _prepare(child, render_help, show_help)
    return command
