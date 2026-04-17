"""Roaming invoke CLI helpers."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import os
from pathlib import Path
from typing import Literal

import click
from rich.console import Console
import typer
from typer import rich_utils
from typer.core import HAS_RICH
from typer.core import TyperArgument, TyperCommand, TyperGroup
from typer.main import get_command

from .. import agents
from .. import up as agent_up
from ..base.error import ToolangError
from ..config.env import load_runtime_environ
from ..execution.runner import RunOutcome
from ..program import ParamDecl
from ..state.durable import scan_durable_state
from ..state.program import LiveProgram, ProgramThunk, build_prepared_program, load_live_program

MarkupMode = Literal["markdown", "rich"]
HELP_FLAGS = {"--help", "-h"}
_TEXT_PART_EXTENSIONS = {".txt", ".md"}
_IMAGE_PART_EXTENSIONS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".bmp",
    ".svg",
}
_AUDIO_PART_EXTENSIONS = {
    ".mp3",
    ".wav",
    ".m4a",
    ".aac",
    ".ogg",
    ".flac",
}


@dataclass(frozen=True, slots=True)
class RoamingInvokeRequest:
    thunk_name: str | None
    input_text: str | None
    models: tuple[str, ...]
    invoke_params: dict[str, object]
    invoke_parts: list[dict[str, str]]


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
    usage_tail = "THUNK [OPTIONS] [PARAMS] [PARTS]"

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
        return [*_help_arguments(show_params=True, show_parts=True), *super().get_params(ctx)]

    def format_commands(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        rows: list[tuple[str, str]] = []
        for subcommand in self.list_commands(ctx):
            cmd = self.get_command(ctx, subcommand)
            if cmd is None or cmd.hidden:
                continue
            limit = formatter.width - 6 - len(subcommand)
            rows.append((subcommand, cmd.get_short_help_str(limit)))
        if rows:
            with formatter.section("Thunks"):
                formatter.write_dl(rows)


class _RoamingThunkHelpCommand(TyperCommand):
    usage_tail = "[OPTIONS]"
    show_params = False
    show_parts = False
    help_thunk: ProgramThunk | None = None

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
        parent_path = ctx.parent.command_path if ctx.parent is not None else ctx.command_path
        command_name = ctx.info_name or self.name or ""
        command_path = f"{parent_path} {command_name}".rstrip()
        formatter.write_usage(command_path, self.usage_tail)

    def get_params(self, ctx: click.Context) -> list[click.Parameter]:
        return [
            *_help_arguments(
                show_params=self.show_params,
                show_parts=self.show_parts,
                thunk=self.help_thunk,
            ),
            *super().get_params(ctx),
        ]


def roaming_source_path(token: str) -> Path | None:
    text = token.strip()
    if not text or text.startswith("-"):
        return None
    candidate = Path(text).expanduser()
    try:
        resolved = candidate.resolve()
    except OSError:
        return None
    if not resolved.is_file() or resolved.suffix != ".too":
        return None
    return resolved


def handle_roaming_invoke(global_args: list[str], body: list[str], *, prog_name: str) -> int:
    if global_args:
        typer.echo("toolang error: too <path>.too does not support global CLI options", err=True)
        return 1
    source_path = roaming_source_path(body[0])
    if source_path is None:
        typer.echo(f"toolang error: agent program not found: {body[0]}", err=True)
        return 1
    source_label = body[0]
    try:
        toolang_root, agent_name, program = _load_roaming_live_program(source_path)
        remaining = body[1:]
        if remaining and remaining[0] in HELP_FLAGS:
            _show_roaming_help(source_label, program, thunk_name=None, prog_name=prog_name)
            return 0
        if not remaining:
            _show_roaming_help(source_label, program, thunk_name=None, prog_name=prog_name)
            return 0
        thunk, remainder = _select_roaming_thunk(program, remaining)
        if any(token in HELP_FLAGS for token in remainder):
            _show_roaming_help(source_label, program, thunk_name=thunk.name, prog_name=prog_name)
            return 0
        request = _parse_roaming_invoke_request(thunk, remainder)
        outcome = agent_up.invoke(
            toolang_root=toolang_root,
            agent_name=agent_name,
            thunk_name=request.thunk_name,
            input_text=request.input_text,
            models=request.models,
            metadata={
                "invoke_params": request.invoke_params,
                "invoke_parts": request.invoke_parts,
            },
            environ=load_runtime_environ(toolang_root, agent_name, base_environ=os.environ),
        )
    except (FileExistsError, FileNotFoundError, ValueError, ToolangError, click.ClickException) as exc:
        message = exc.message if isinstance(exc, click.ClickException) else str(exc)
        typer.echo(f"toolang error: {message}", err=True)
        return 1
    return _emit_invoke_outcome(outcome)


def _load_roaming_live_program(source_path: Path) -> tuple[Path, str, LiveProgram]:
    toolang_root, agent_name = agents.materialize_roaming_program(source_path)
    durable = scan_durable_state(toolang_root, agent_name)
    prepared = build_prepared_program(durable)
    return toolang_root, agent_name, load_live_program(prepared)


def _select_roaming_thunk(
    program: LiveProgram,
    argv: list[str],
) -> tuple[ProgramThunk, list[str]]:
    for thunk in program.thunks:
        if thunk.name == argv[0]:
            return thunk, argv[1:]
    raise click.ClickException(f"unknown thunk: {argv[0]}")


def _parse_roaming_invoke_request(
    thunk: ProgramThunk,
    argv: list[str],
) -> RoamingInvokeRequest:
    param_index = {param.name: param for param in thunk.params}
    invoke_params: dict[str, object] = {}
    parts: list[str] = []
    models: list[str] = []
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--":
            parts.extend(argv[index + 1 :])
            break
        if token.startswith("--model="):
            model = token.partition("=")[2].strip()
            if not model:
                raise click.ClickException("--model requires a value")
            models.append(model)
            index += 1
            continue
        if token == "--model":
            if index + 1 >= len(argv):
                raise click.ClickException("--model requires a value")
            model = argv[index + 1].strip()
            if not model:
                raise click.ClickException("--model requires a value")
            models.append(model)
            index += 2
            continue
        if token.startswith("--"):
            raise click.ClickException(f"unknown Toolang invoke option: {token}")
        param_name, has_assignment, raw_value = token.partition("=")
        param = param_index.get(param_name) if has_assignment else None
        if param is not None:
            if param_name in invoke_params:
                raise click.ClickException(f"duplicate invoke parameter: {param_name}")
            invoke_params[param_name] = _coerce_invoke_value(raw_value, thunk_param=param)
            index += 1
            continue
        parts.append(token)
        index += 1
    missing = [param.name for param in thunk.params if not param.optional and param.name not in invoke_params]
    if missing:
        joined = ", ".join(f"{name}=..." for name in missing)
        raise click.ClickException(f"missing required invoke parameters: {joined}")
    if thunk.accepts_message and not parts:
        raise click.ClickException(f"thunk {thunk.name!r} requires at least one PART")
    if parts and not thunk.accepts_message:
        raise click.ClickException(f"thunk {thunk.name!r} does not accept message input")
    input_text, invoke_parts = _render_roaming_input(parts) if parts else (None, [])
    return RoamingInvokeRequest(
        thunk_name=thunk.name,
        input_text=input_text,
        models=tuple(models),
        invoke_params=invoke_params,
        invoke_parts=invoke_parts,
    )


def _parse_boolean_value(raw: str, *, option_name: str) -> bool:
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise click.ClickException(f"{option_name} expects a boolean value")


def _coerce_invoke_value(raw: str, *, thunk_param: ParamDecl) -> object:
    type_name = thunk_param.type_name
    if type_name == "number":
        try:
            if any(marker in raw for marker in (".", "e", "E")):
                return float(raw)
            return int(raw)
        except ValueError as exc:
            raise click.ClickException(f"{thunk_param.name} expects a number") from exc
    if type_name == "boolean":
        return _parse_boolean_value(raw, option_name=thunk_param.name)
    if type_name == "path":
        return str(Path(raw).expanduser().resolve())
    return raw


def _render_roaming_input(parts: list[str]) -> tuple[str, list[dict[str, str]]]:
    rendered: list[str] = []
    invoke_parts: list[dict[str, str]] = []
    for part in parts:
        if part.startswith("@@"):
            text = part[1:]
            rendered.append(text)
            invoke_parts.append({"type": "text", "text": text})
            continue
        if part.startswith("@"):
            candidate = Path(part[1:]).expanduser().resolve()
            if not candidate.exists():
                raise click.ClickException(f"invoke part not found: {candidate}")
            ext = candidate.suffix.lower()
            if ext in _TEXT_PART_EXTENSIONS:
                text = candidate.read_text(encoding="utf-8")
                rendered.append(text)
                invoke_parts.append({"type": "text", "text": text, "path": str(candidate)})
                continue
            part_type = _path_part_type(candidate)
            rendered.append(f"Attached {part_type}: {candidate}")
            invoke_parts.append({"type": part_type, "path": str(candidate)})
            continue
        rendered.append(part)
        invoke_parts.append({"type": "text", "text": part})
    return "\n\n".join(rendered), invoke_parts


def _path_part_type(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in _IMAGE_PART_EXTENSIONS:
        return "image"
    if ext in _AUDIO_PART_EXTENSIONS:
        return "audio"
    return "file"


def _emit_invoke_outcome(outcome: RunOutcome) -> int:
    if outcome.status == "failed":
        typer.echo(f"toolang error: {outcome.error or 'invoke failed'}", err=True)
        return 1
    if outcome.output_text:
        typer.echo(outcome.output_text)
    return 0


def _show_roaming_help(
    source_label: str,
    program: LiveProgram,
    *,
    thunk_name: str | None,
    prog_name: str,
) -> None:
    app = _build_roaming_help_app(source_label, program)
    command = get_command(app)
    if not isinstance(command, _RoamingInvokeHelpGroup):
        raise RuntimeError("expected roaming help group")
    args = ["--help"] if thunk_name is None else [thunk_name, "--help"]
    try:
        command.main(
            args=args,
            prog_name=f"{prog_name} {source_label}",
            standalone_mode=False,
        )
    except click.exceptions.Exit:
        return


def _build_roaming_help_app(source_label: str, program: LiveProgram) -> typer.Typer:
    app = typer.Typer(
        cls=_RoamingInvokeHelpGroup,
        add_completion=False,
        no_args_is_help=True,
        invoke_without_command=True,
        pretty_exceptions_enable=False,
        pretty_exceptions_show_locals=False,
        help=f"Invoke thunks from: {source_label}",
    )

    @app.callback()
    def _callback() -> None:
        return None

    for thunk in program.thunks:
        app.command(
            thunk.name,
            help=_thunk_summary(thunk),
            cls=_make_roaming_thunk_help_command_class(thunk),
            rich_help_panel="Thunks",
        )(_make_roaming_help_command())
    return app


def _make_roaming_thunk_help_command_class(thunk: ProgramThunk) -> type[_RoamingThunkHelpCommand]:
    class _ConfiguredRoamingThunkHelpCommand(_RoamingThunkHelpCommand):
        usage_tail = _roaming_thunk_usage_tail(thunk)
        show_params = bool(thunk.params)
        show_parts = thunk.accepts_message
        help_thunk = thunk

    return _ConfiguredRoamingThunkHelpCommand


def _make_roaming_help_command() -> Callable[..., None]:
    def command(
        model: list[str] | None = typer.Option(
            None,
            "--model",
            help="Allow a model selector for this activation. Repeat to allow multiple; the first becomes default.",
        ),
    ) -> None:
        del model
        return None

    return command


def _roaming_thunk_usage_tail(thunk: ProgramThunk) -> str:
    pieces = ["[OPTIONS]"]
    if thunk.params:
        pieces.append("[PARAMS]")
    if thunk.accepts_message:
        pieces.append("PARTS")
    return " ".join(pieces)


def _param_assignment_label(param: ParamDecl) -> str:
    type_name = param.type_name or "TEXT"
    if param.type_name == "number":
        type_name = "NUMBER"
    elif param.type_name == "boolean":
        type_name = "BOOLEAN"
    elif param.type_name == "path":
        type_name = "PATH"
    elif type_name.islower():
        type_name = type_name.upper()
    return f"{param.name}={type_name}"


def _thunk_summary(thunk: ProgramThunk) -> str:
    for line in thunk.body.splitlines():
        text = line.strip()
        if text:
            return text
    return "-"


def _help_arguments(
    *,
    show_params: bool,
    show_parts: bool,
    thunk: ProgramThunk | None = None,
) -> list[click.Parameter]:
    args: list[click.Parameter] = []
    if show_params:
        if thunk is None or not thunk.params:
            args.append(
                _HelpOnlyArgument(
                    param_decls=["params"],
                    metavar="NAME=VALUE",
                    required=False,
                    default=None,
                    expose_value=False,
                    help="One named thunk parameter. Repeat as needed.",
                    rich_help_panel="Params",
                )
            )
        else:
            for param in thunk.params:
                required = "required" if not param.optional else "optional"
                args.append(
                    _HelpOnlyArgument(
                        param_decls=[f"param_{param.name}"],
                        metavar=_param_assignment_label(param),
                        required=False,
                        default=None,
                        expose_value=False,
                        help=f"{param.type_name or 'string'}; {required}.",
                        rich_help_panel="Params",
                    )
                )
    if show_parts:
        args.extend(
            [
                _HelpOnlyArgument(
                    param_decls=["part_text"],
                    metavar="TEXT",
                    required=False,
                    default=None,
                    expose_value=False,
                    help="Plain text. Use @@TEXT for literal text starting with @.",
                    rich_help_panel="Parts",
                ),
                _HelpOnlyArgument(
                    param_decls=["part_text_file"],
                    metavar="@FILE.md",
                    required=False,
                    default=None,
                    expose_value=False,
                    help="Text loaded from a .md file. Also supports .txt.",
                    rich_help_panel="Parts",
                ),
                _HelpOnlyArgument(
                    param_decls=["part_image"],
                    metavar="@FILE.png",
                    required=False,
                    default=None,
                    expose_value=False,
                    help="Image loaded from a .png file. Also supports .jpg, .jpeg, .gif, .webp, .bmp, and .svg.",
                    rich_help_panel="Parts",
                ),
                _HelpOnlyArgument(
                    param_decls=["part_audio"],
                    metavar="@FILE.mp3",
                    required=False,
                    default=None,
                    expose_value=False,
                    help="Audio loaded from a .mp3 file. Also supports .wav, .m4a, .aac, .ogg, and .flac.",
                    rich_help_panel="Parts",
                ),
                _HelpOnlyArgument(
                    param_decls=["part_file"],
                    metavar="@FILE",
                    required=False,
                    default=None,
                    expose_value=False,
                    help="Generic file loaded from any other file type.",
                    rich_help_panel="Parts",
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
    parts_args: list[click.Argument] = []
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
            elif panel_name == "Parts":
                parts_args.append(param)

    rich_utils._print_options_panel(
        name=rich_utils.OPTIONS_PANEL_TITLE,
        params=options,
        ctx=ctx,
        markup_mode=markup_mode,
        console=console,
    )

    if show_commands and isinstance(obj, click.Group):
        commands = [command for name in obj.list_commands(ctx) if (command := obj.get_command(ctx, name)) and not command.hidden]
        max_cmd_len = max((len(command.name or "") for command in commands), default=0)
        rich_utils._print_commands_panel(
            name="Thunks",
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
        name="Parts",
        params=parts_args,
        ctx=ctx,
        markup_mode=markup_mode,
        console=console,
    )

    if obj.epilog:
        lines = obj.epilog.split("\n\n")
        epilogue = "\n".join([line.replace("\n", " ").strip() for line in lines])
        epilogue_text = rich_utils._make_rich_text(text=epilogue, markup_mode=markup_mode)
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
            rich_utils._get_parameter_help(param=param, ctx=ctx, markup_mode=markup_mode),
        )
    console.print(
        rich_utils.Panel(
            table,
            border_style=rich_utils.STYLE_OPTIONS_PANEL_BORDER,
            title=name,
            title_align=rich_utils.ALIGN_OPTIONS_PANEL,
        )
    )
