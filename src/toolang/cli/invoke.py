"""Roaming invoke CLI helpers."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import os
from pathlib import Path
import sys
from typing import Literal

import click
from rich.console import Console
from rich.live import Live
from rich.text import Text
import typer
from typer import rich_utils
from typer.core import HAS_RICH
from typer.core import TyperArgument, TyperCommand, TyperGroup
from typer.main import get_command

from .. import agents
from .. import up as agent_up
from ..base.error import ToolangError
from ..config.env import load_runtime_environ
from ..config.log_spec import PY_LOG_ENV_VAR
from ..execution.events import RunEnd, RunStart, StepEnd, StepStart, TraceEvent
from ..execution.runner import RunOutcome
from ..program import ParamDecl, Thunk
from ..state.prepared import PreparedState
from ..state.program import LiveProgram, load_live_program
from .progress import CliProgress, as_progress_sink, make_cli_progress

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
    quiet: bool = False


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


class _MissingInvokeInput(click.ClickException):
    pass


class _RoamingInvokeHelpGroup(TyperGroup):
    usage_tail = "THUNK [OPTIONS] [PARAMS] [INPUT]..."

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
                show_thunk=False,
                show_params=True,
                show_parts=True,
                show_input_forms=True,
            ),
            *super().get_params(ctx),
        ]

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
    help_thunk: Thunk | None = None

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
                show_thunk=False,
                show_params=self.show_params,
                show_parts=self.show_parts,
                show_input_forms=True,
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
    if _unsupported_roaming_global_args(global_args):
        typer.echo(
            "toolang error: too <path>.too does not support global CLI options",
            err=True,
        )
        return 1
    source_path = roaming_source_path(body[0])
    if source_path is None:
        typer.echo(f"toolang error: agent program not found: {body[0]}", err=True)
        return 1
    source_label = body[0]
    remaining = body[1:]
    quiet, leading_models, normalized_remaining = _consume_roaming_control_options(remaining)
    prepare_progress = _prepare_progress(quiet=quiet, argv=remaining)
    script_progress: _ScriptProgressSink | None = None
    request: RoamingInvokeRequest | None = None
    runtime_environ: dict[str, str] | None = None
    toolang_root: Path | None = None
    agent_name: str | None = None
    try:
        toolang_root, agent_name, prepared, program = _load_roaming_live_program(
            source_path,
            progress=as_progress_sink(prepare_progress),
        )
        if prepare_progress is not None:
            prepare_progress.finish(details=False)
        if normalized_remaining and normalized_remaining[0] in HELP_FLAGS:
            _show_roaming_help(source_label, program, thunk_name=None, prog_name=prog_name)
            return 0
        if not remaining:
            _show_roaming_help(source_label, program, thunk_name=None, prog_name=prog_name)
            return 0
        if not normalized_remaining:
            _show_roaming_help(source_label, program, thunk_name=None, prog_name=prog_name)
            return 0
        thunk, remainder = _select_roaming_thunk(program, normalized_remaining)
        if any(token in HELP_FLAGS for token in remainder):
            _show_roaming_help(source_label, program, thunk_name=_thunk_name(thunk), prog_name=prog_name)
            return 0
        try:
            request = _parse_roaming_invoke_request(thunk, remainder, leading_models=leading_models)
        except _MissingInvokeInput:
            _show_roaming_help(source_label, program, thunk_name=_thunk_name(thunk), prog_name=prog_name)
            return 0
        runtime_environ = load_runtime_environ(toolang_root, agent_name, base_environ=os.environ)
        script_progress = _script_progress_sink(thunk_name=request.thunk_name, quiet=quiet or request.quiet)
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
            environ=runtime_environ,
            response=script_progress,
            prepared_state=prepared,
        )
    except KeyboardInterrupt:
        if script_progress is not None:
            script_progress.interrupt()
        if prepare_progress is not None:
            prepare_progress.interrupt()
        _emit_interrupt_message(
            script_progress=script_progress,
            toolang_root=toolang_root,
            agent_name=agent_name,
            thunk_name=request.thunk_name if request is not None else None,
            environ=runtime_environ,
        )
        return 130
    except (FileExistsError, FileNotFoundError, ValueError, ToolangError, click.ClickException) as exc:
        if prepare_progress is not None:
            prepare_progress.finish(details=False)
        message = exc.message if isinstance(exc, click.ClickException) else str(exc)
        typer.echo(f"toolang error: {message}", err=True)
        return 1
    return _emit_invoke_outcome(outcome)


def _unsupported_roaming_global_args(global_args: list[str]) -> bool:
    return bool(global_args)


def _consume_roaming_control_options(argv: list[str]) -> tuple[bool, tuple[str, ...], list[str]]:
    quiet = False
    models: list[str] = []
    remaining: list[str] = []
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--":
            remaining.extend(argv[index:])
            break
        if token in {"--quiet", "-q"}:
            quiet = True
            index += 1
            continue
        if token.startswith("--model="):
            model = token.partition("=")[2].strip()
            if model:
                models.append(model)
                index += 1
                continue
        if token == "--model" and index + 1 < len(argv):
            model = argv[index + 1].strip()
            if model:
                models.append(model)
                index += 2
                continue
        remaining.append(token)
        index += 1
    return quiet, tuple(models), remaining


def _prepare_progress(*, quiet: bool, argv: list[str]) -> "CliProgress | None":
    if quiet or not sys.stderr.isatty() or any(token in HELP_FLAGS for token in argv):
        return None
    return make_cli_progress()


def _load_roaming_live_program(
    source_path: Path,
    *,
    progress=None,
) -> tuple[Path, str, PreparedState, LiveProgram]:
    toolang_root, agent_name = agents.materialize_roaming_program(source_path)
    prepared = agent_up.prepare_agent(toolang_root=toolang_root, agent_name=agent_name, progress=progress)
    return toolang_root, agent_name, prepared, load_live_program(prepared.program)


def _select_roaming_thunk(
    program: LiveProgram,
    argv: list[str],
) -> tuple[Thunk, list[str]]:
    for thunk in program.thunks:
        if _thunk_name(thunk) == argv[0]:
            return thunk, argv[1:]
    raise click.ClickException(f"unknown thunk: {argv[0]}")


def _parse_roaming_invoke_request(
    thunk: Thunk,
    argv: list[str],
    *,
    leading_models: tuple[str, ...] = (),
) -> RoamingInvokeRequest:
    thunk_params = tuple(thunk.params)
    param_index = {param.name: param for param in thunk_params}
    invoke_params: dict[str, object] = {}
    parts: list[str] = []
    models = list(leading_models)
    quiet = False
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
        if token in {"--quiet", "-q"}:
            quiet = True
            index += 1
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
    missing = [param.name for param in thunk_params if not param.optional and param.name not in invoke_params]
    if missing:
        joined = ", ".join(f"{name}=..." for name in missing)
        raise click.ClickException(f"missing required invoke parameters: {joined}")
    thunk_name = _thunk_name(thunk)
    accepts_message = thunk.input is not None
    if accepts_message and not parts:
        raise _MissingInvokeInput(f"thunk {thunk_name!r} requires at least one INPUT")
    if parts and not accepts_message:
        raise click.ClickException(f"thunk {thunk_name!r} does not accept INPUT")
    input_text, invoke_parts = _render_roaming_input(parts) if parts else (None, [])
    return RoamingInvokeRequest(
        thunk_name=thunk_name,
        input_text=input_text,
        models=tuple(models),
        invoke_params=invoke_params,
        invoke_parts=invoke_parts,
        quiet=quiet,
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
                raise click.ClickException(f"invoke input not found: {candidate}")
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
        typer.echo(f"Run: {outcome.run_id}", err=True)
        if outcome.log_path:
            typer.echo(f"Log: {outcome.log_path}", err=True)
        return 1
    if outcome.output_text:
        typer.echo(outcome.output_text)
    return 0


def _script_progress_sink(*, thunk_name: str | None, quiet: bool) -> "_ScriptProgressSink":
    return _ScriptProgressSink(
        thunk_name=thunk_name or "main",
        render=not quiet and sys.stderr.isatty(),
    )


def _emit_interrupt_message(
    *,
    script_progress: "_ScriptProgressSink | None",
    toolang_root: Path | None,
    agent_name: str | None,
    thunk_name: str | None,
    environ: dict[str, str] | None,
) -> None:
    typer.echo("toolang interrupted", err=True)
    run_id = script_progress.run_id if script_progress is not None else None
    if run_id:
        typer.echo(f"Run: {run_id}", err=True)
    if not run_id or toolang_root is None or agent_name is None:
        return
    if environ is None or not environ.get(PY_LOG_ENV_VAR, "").strip():
        return
    typer.echo(
        f"Log: {agents.agent_script_run_log_path(toolang_root, agent_name, thunk_name=thunk_name, run_id=run_id)}",
        err=True,
    )


class _ScriptProgressSink:
    """Render script progress to stderr without touching stdout."""

    wants_stream = False

    def __init__(self, *, thunk_name: str, render: bool) -> None:
        self._thunk_name = thunk_name
        self._render_enabled = render
        self._run_id: str | None = None
        self._steps: dict[int, tuple[str, str]] = {}
        self._console = Console(file=sys.stderr, force_terminal=True, highlight=False)
        self._live: Live | None = None

    @property
    def run_id(self) -> str | None:
        return self._run_id

    def on_event(self, event: TraceEvent) -> None:
        if isinstance(event, RunStart):
            self._run_id = event.run_id
            self._render(f"Running {self._thunk_name}: {event.run_id}")
            return
        if isinstance(event, StepStart):
            self._steps[event.step_index] = (event.kind, "running")
            self._render(f"{self._label(event.step_index, event.kind)} running")
            return
        if isinstance(event, StepEnd):
            self._steps[event.step_index] = (event.kind, event.status)
            self._render(f"{self._label(event.step_index, event.kind)} {event.status}")
            return
        if isinstance(event, RunEnd):
            self.finish()

    def finish(self) -> None:
        if self._live is None:
            return
        self._live.stop()
        self._live = None

    def interrupt(self) -> None:
        self.finish()

    def _label(self, step_index: int, kind: str) -> str:
        return f"{self._thunk_name} step {step_index} {kind}"

    def _render(self, message: str) -> None:
        if not self._render_enabled:
            return
        text = Text(message, style="dim")
        if self._live is None:
            self._live = Live(
                text,
                console=self._console,
                refresh_per_second=10,
                transient=True,
            )
            self._live.start(refresh=True)
            return
        self._live.update(text, refresh=True)


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
            prog_name=f"{prog_name} SCRIPT.too",
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
        help=f"Invoke a thunk from a Toolang script.\n\nScript: {source_label}",
    )

    @app.callback()
    def _callback(
        model: list[str] | None = typer.Option(
            None,
            "--model",
            help="Model selector. Repeat to allow multiple.",
        ),
        quiet: bool = typer.Option(
            False,
            "--quiet",
            "-q",
            help="Suppress progress messages.",
        ),
    ) -> None:
        del model, quiet
        return None

    for thunk in program.thunks:
        app.command(
            _thunk_name(thunk),
            help=_roaming_thunk_help_text(source_label, thunk),
            short_help=_thunk_summary(thunk),
            cls=_make_roaming_thunk_help_command_class(thunk),
            rich_help_panel="Thunks",
        )(_make_roaming_help_command())
    return app


def _roaming_thunk_help_text(source_label: str, thunk: Thunk) -> str:
    summary = _thunk_summary(thunk)
    intro = "Invoke a thunk from a Toolang script." if summary == "-" else summary
    return f"{intro}\n\nScript: {source_label}\nThunk:  {_thunk_name(thunk)}"


def _make_roaming_thunk_help_command_class(thunk: Thunk) -> type[_RoamingThunkHelpCommand]:
    class _ConfiguredRoamingThunkHelpCommand(_RoamingThunkHelpCommand):
        usage_tail = _roaming_thunk_usage_tail(thunk)
        show_params = bool(thunk.params)
        show_parts = thunk.input is not None
        help_thunk = thunk

    return _ConfiguredRoamingThunkHelpCommand


def _make_roaming_help_command() -> Callable[..., None]:
    def command(
        model: list[str] | None = typer.Option(
            None,
            "--model",
            help="Model selector. Repeat to allow multiple.",
        ),
        quiet: bool = typer.Option(
            False,
            "--quiet",
            "-q",
            help="Suppress progress messages.",
        ),
    ) -> None:
        del model, quiet
        return None

    return command


def _roaming_thunk_usage_tail(thunk: Thunk) -> str:
    pieces = ["[OPTIONS]"]
    if thunk.params:
        pieces.append("[PARAMS]")
    if thunk.input is not None:
        pieces.append("[INPUT]...")
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


def _thunk_summary(thunk: Thunk) -> str:
    for line in thunk.messages_text().splitlines():
        text = line.strip()
        if text:
            return text
    return "-"


def _help_arguments(
    *,
    show_thunk: bool,
    show_params: bool,
    show_parts: bool,
    show_input_forms: bool,
    thunk: Thunk | None = None,
) -> list[click.Parameter]:
    args: list[click.Parameter] = []
    if show_thunk:
        args.append(
            _HelpOnlyArgument(
                param_decls=["thunk"],
                metavar="THUNK",
                required=False,
                default=None,
                expose_value=False,
                help="Thunk to invoke.",
                rich_help_panel="Arguments",
            )
        )
    if show_params:
        thunk_params = () if thunk is None else tuple(thunk.params)
        if not thunk_params:
            args.append(
                _HelpOnlyArgument(
                    param_decls=["params"],
                    metavar="NAME=VALUE",
                    required=False,
                    default=None,
                    expose_value=False,
                    help="Set one named thunk parameter. Repeat as needed.",
                    rich_help_panel="Params",
                )
            )
        else:
            for param in thunk_params:
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


def _thunk_name(thunk: Thunk) -> str:
    return thunk.name or "main"


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
        name="Input",
        params=input_args,
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
