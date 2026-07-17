"""Dynamic help for script executables."""

from __future__ import annotations

from collections.abc import Callable
from typing import Literal

import click
from rich.console import Console
import typer
from typer import rich_utils
from typer.core import HAS_RICH
from typer.core import TyperArgument, TyperCommand, TyperGroup
from typer.main import get_command

from toolang.lang.ast import AgicDecl, FlowDecl, Parameter, Program
from .request import default_agic_name, executable_name

MarkupMode = Literal["markdown", "rich"]


class _HelpOnlyArgument(TyperArgument):
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


class _RoamingInvokeHelpGroup(TyperGroup):
    usage_tail = "TARGET [OPTIONS] [PARAMS] [INPUT]..."

    def format_help(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        if not HAS_RICH or self.rich_markup_mode is None:
            return super().format_help(ctx, formatter)
        _rich_format_roaming_help(
            obj=self,
            ctx=ctx,
            markup_mode=self.rich_markup_mode,
            show_commands=True,
        )

    def format_usage(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        formatter.write_usage(ctx.command_path, self.usage_tail)

    def get_params(self, ctx: click.Context) -> list[click.Parameter]:
        return [
            *_help_arguments(
                show_agic=False,
                show_params=True,
                show_parts=True,
                show_input_forms=True,
            ),
            *super().get_params(ctx),
        ]

    def format_commands(
        self, ctx: click.Context, formatter: click.HelpFormatter
    ) -> None:
        rows: list[tuple[str, str]] = []
        for subcommand in self.list_commands(ctx):
            cmd = self.get_command(ctx, subcommand)
            if cmd is None or cmd.hidden:
                continue
            limit = formatter.width - 6 - len(subcommand)
            rows.append((subcommand, cmd.get_short_help_str(limit)))
        if rows:
            with formatter.section("Targets"):
                formatter.write_dl(rows)


class _RoamingAgicHelpCommand(TyperCommand):
    usage_tail = "[OPTIONS]"
    show_params = False
    show_parts = False
    help_executable: AgicDecl | FlowDecl | None = None

    def format_help(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        if not HAS_RICH or self.rich_markup_mode is None:
            return super().format_help(ctx, formatter)
        _rich_format_roaming_help(
            obj=self,
            ctx=ctx,
            markup_mode=self.rich_markup_mode,
            show_commands=False,
        )

    def format_usage(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        parent_path = (
            ctx.parent.command_path if ctx.parent is not None else ctx.command_path
        )
        command_path = f"{parent_path} TARGET".rstrip()
        formatter.write_usage(command_path, self.usage_tail)

    def get_params(self, ctx: click.Context) -> list[click.Parameter]:
        return [
            *_help_arguments(
                show_agic=False,
                show_params=self.show_params,
                show_parts=self.show_parts,
                show_input_forms=True,
                executable=self.help_executable,
            ),
            *super().get_params(ctx),
        ]


def show_help(
    source_label: str,
    program: Program,
    *,
    target_name: str | None,
    prog_name: str,
) -> None:
    app = _build_roaming_help_app(source_label, program)
    command = get_command(app)
    if not isinstance(command, _RoamingInvokeHelpGroup):
        raise RuntimeError("expected roaming help group")
    args = ["--help"] if target_name is None else [target_name, "--help"]
    try:
        command.main(
            args=args,
            prog_name=f"{prog_name} SCRIPT",
            standalone_mode=False,
        )
    except click.exceptions.Exit:
        return


def _build_roaming_help_app(source_label: str, program: Program) -> typer.Typer:
    app = typer.Typer(
        cls=_RoamingInvokeHelpGroup,
        add_completion=False,
        no_args_is_help=True,
        invoke_without_command=True,
        pretty_exceptions_enable=False,
        pretty_exceptions_show_locals=False,
        help=f"Invoke an agic or flow from a Toolang script.\n\nScript: {source_label}",
    )

    @app.callback()
    def _callback(
        model: list[str] | None = typer.Option(
            None,
            "--models",
            help="Limit available models. Pass CSV or repeat.",
        ),
        tools: list[str] | None = typer.Option(
            None,
            "--tools",
            help="Allow selected tools. Pass CSV or repeat.",
        ),
        caps: list[str] | None = typer.Option(
            None,
            "--caps",
            help="Allow selected caps. Pass CSV or repeat.",
        ),
        quiet: bool = typer.Option(
            False,
            "--quiet",
            "-q",
            help="Suppress progress messages.",
        ),
    ) -> None:
        del model, tools, caps, quiet
        return None

    for agic in program.available_agics:
        app.command(
            default_agic_name(agic),
            help=_roaming_executable_help_text(source_label, agic),
            short_help=_executable_summary(agic),
            cls=_make_roaming_executable_help_command_class(agic),
            rich_help_panel="Agics",
        )(_make_roaming_help_command())
    for flow in program.flows:
        app.command(
            flow.name,
            help=_roaming_executable_help_text(source_label, flow),
            short_help=_executable_summary(flow),
            cls=_make_roaming_executable_help_command_class(flow),
            rich_help_panel="Flows",
        )(_make_roaming_help_command())
    return app


def _roaming_executable_help_text(
    source_label: str, executable: AgicDecl | FlowDecl
) -> str:
    summary = _executable_summary(executable)
    intro = (
        "Invoke an agic or flow from a Toolang script." if summary == "-" else summary
    )
    label = "Agic" if isinstance(executable, AgicDecl) else "Flow"
    return f"{intro}\n\nScript: {source_label}\n{label}:  {executable_name(executable)}"


def _make_roaming_executable_help_command_class(
    executable: AgicDecl | FlowDecl,
) -> type[_RoamingAgicHelpCommand]:
    class _ConfiguredRoamingAgicHelpCommand(_RoamingAgicHelpCommand):
        usage_tail = _roaming_executable_usage_tail(executable)
        show_params = bool(executable.params)
        show_parts = executable.input is not None
        help_executable = executable

    return _ConfiguredRoamingAgicHelpCommand


def _make_roaming_help_command() -> Callable[..., None]:
    def command(
        model: list[str] | None = typer.Option(
            None,
            "--models",
            help="Limit available models. Pass CSV or repeat.",
        ),
        tools: list[str] | None = typer.Option(
            None,
            "--tools",
            help="Allow selected tools. Pass CSV or repeat.",
        ),
        caps: list[str] | None = typer.Option(
            None,
            "--caps",
            help="Allow selected caps. Pass CSV or repeat.",
        ),
        quiet: bool = typer.Option(
            False,
            "--quiet",
            "-q",
            help="Suppress progress messages.",
        ),
    ) -> None:
        del model, tools, caps, quiet
        return None

    return command


def _roaming_executable_usage_tail(executable: AgicDecl | FlowDecl) -> str:
    pieces = ["[OPTIONS]"]
    if executable.params:
        pieces.append("[PARAMS]")
    if executable.input is not None:
        pieces.append("[INPUT]...")
    return " ".join(pieces)


def _param_assignment_label(param: Parameter) -> str:
    type_name = param.type_name or "TEXT"
    if param.type_name == "Number":
        type_name = "NUMBER"
    elif param.type_name == "Boolean":
        type_name = "BOOLEAN"
    elif param.type_name == "Path":
        type_name = "PATH"
    elif type_name.islower():
        type_name = type_name.upper()
    return f"{param.name}={type_name}"


def _executable_summary(executable: AgicDecl | FlowDecl) -> str:
    if isinstance(executable, AgicDecl):
        for line in "\n\n".join(
            item.content for item in executable.messages
        ).splitlines():
            text = line.strip()
            if text:
                return text
    if isinstance(executable, FlowDecl) and executable.stmts:
        return f"{len(executable.stmts)} flow statements"
    return "-"


def _help_arguments(
    *,
    show_agic: bool,
    show_params: bool,
    show_parts: bool,
    show_input_forms: bool,
    executable: AgicDecl | FlowDecl | None = None,
) -> list[click.Parameter]:
    args: list[click.Parameter] = []
    if show_agic:
        args.append(
            _HelpOnlyArgument(
                param_decls=["target"],
                metavar="TARGET",
                required=False,
                default=None,
                expose_value=False,
                help="Agic or flow to invoke.",
                rich_help_panel="Arguments",
            )
        )
    if show_params:
        executable_params = () if executable is None else tuple(executable.params)
        if not executable_params:
            args.append(
                _HelpOnlyArgument(
                    param_decls=["params"],
                    metavar="NAME=VALUE",
                    required=False,
                    default=None,
                    expose_value=False,
                    help="Set one named agic parameter. Repeat as needed.",
                    rich_help_panel="Params",
                )
            )
        else:
            for param in executable_params:
                required = "required" if not param.optional else "optional"
                args.append(
                    _HelpOnlyArgument(
                        param_decls=[f"param_{param.name}"],
                        metavar=_param_assignment_label(param),
                        required=False,
                        default=None,
                        expose_value=False,
                        help=f"{param.type_name or 'Text'}; {required}.",
                        rich_help_panel="Params",
                    )
                )
    if show_parts:
        if show_input_forms:
            args.extend(
                [
                    _HelpOnlyArgument(
                        param_decls=["part_text"],
                        metavar="TEXT",
                        required=False,
                        default=None,
                        expose_value=False,
                        help="Text part. Use @@TEXT for literal text starting with @.",
                        rich_help_panel="Input",
                    ),
                    _HelpOnlyArgument(
                        param_decls=["part_file"],
                        metavar="@PATH",
                        required=False,
                        default=None,
                        expose_value=False,
                        help="File input. Modality is inferred from the extension.",
                        rich_help_panel="Input",
                    ),
                ]
            )
    return args


def _rich_format_roaming_help(
    *,
    obj: click.Command | click.Group,
    ctx: click.Context,
    markup_mode: MarkupMode,
    show_commands: bool,
) -> None:
    console = rich_utils._get_rich_console()
    console.print(
        rich_utils.Padding(rich_utils.highlighter(obj.get_usage(ctx)), 1),
        style=rich_utils.STYLE_USAGE_COMMAND,
    )
    if obj.help:
        console.print(
            rich_utils.Padding(
                rich_utils.Align(
                    rich_utils._get_help_text(obj=obj, markup_mode=markup_mode),
                    pad=False,
                ),
                (0, 1, 1, 1),
            )
        )

    options: list[click.Option] = []
    params_args: list[click.Argument] = []
    input_args: list[click.Argument] = []
    for param in obj.get_params(ctx):
        if getattr(param, "hidden", False):
            continue
        if isinstance(param, click.Option):
            options.append(param)
            continue
        if isinstance(param, click.Argument):
            panel_name = getattr(param, rich_utils._RICH_HELP_PANEL_NAME, None)
            if panel_name == "Params":
                params_args.append(param)
            elif panel_name == "Input":
                input_args.append(param)

    rich_utils._print_options_panel(
        name=rich_utils.OPTIONS_PANEL_TITLE,
        params=options,
        ctx=ctx,
        markup_mode=markup_mode,
        console=console,
    )

    if show_commands and isinstance(obj, click.Group):
        commands = [
            command
            for name in obj.list_commands(ctx)
            if (command := obj.get_command(ctx, name)) and not command.hidden
        ]
        max_cmd_len = max((len(command.name or "") for command in commands), default=0)
        rich_utils._print_commands_panel(
            name="Targets",
            commands=commands,
            markup_mode=markup_mode,
            console=console,
            cmd_len=max_cmd_len,
        )

    _print_argument_examples_panel(
        name="Params",
        params=params_args,
        ctx=ctx,
        markup_mode=markup_mode,
        console=console,
    )
    _print_argument_examples_panel(
        name="Input",
        params=input_args,
        ctx=ctx,
        markup_mode=markup_mode,
        console=console,
    )

    if obj.epilog:
        lines = obj.epilog.split("\n\n")
        epilogue = "\n".join([line.replace("\n", " ").strip() for line in lines])
        epilogue_text = rich_utils._make_rich_text(
            text=epilogue, markup_mode=markup_mode
        )
        console.print(rich_utils.Padding(rich_utils.Align(epilogue_text, pad=False), 1))


def _print_argument_examples_panel(
    *,
    name: str,
    params: list[click.Argument],
    ctx: click.Context,
    markup_mode: MarkupMode,
    console: Console,
) -> None:
    if not params:
        return
    table = rich_utils.Table(
        highlight=True,
        show_header=False,
        expand=True,
        box=getattr(rich_utils.box, rich_utils.STYLE_OPTIONS_TABLE_BOX, None),
        show_lines=rich_utils.STYLE_OPTIONS_TABLE_SHOW_LINES,
        leading=rich_utils.STYLE_OPTIONS_TABLE_LEADING,
        border_style=rich_utils.STYLE_OPTIONS_TABLE_BORDER_STYLE,
        row_styles=rich_utils.STYLE_OPTIONS_TABLE_ROW_STYLES,
        pad_edge=rich_utils.STYLE_OPTIONS_TABLE_PAD_EDGE,
        padding=rich_utils.STYLE_OPTIONS_TABLE_PADDING,
    )
    table.add_column(style=rich_utils.STYLE_METAVAR, no_wrap=True)
    table.add_column(ratio=10)
    for param in params:
        table.add_row(
            rich_utils.metavar_highlighter(param.make_metavar(ctx=ctx)),
            rich_utils._get_parameter_help(
                param=param, ctx=ctx, markup_mode=markup_mode
            ),
        )
    console.print(
        rich_utils.Panel(
            table,
            border_style=rich_utils.STYLE_OPTIONS_PANEL_BORDER,
            title=name,
            title_align=rich_utils.ALIGN_OPTIONS_PANEL,
        )
    )
