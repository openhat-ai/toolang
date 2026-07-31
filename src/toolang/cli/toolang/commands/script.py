"""Run authored agics and flows from one local Toolang script."""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Callable, Mapping
import json
import mimetypes
import os
from pathlib import Path
import sys
from typing import Any, TextIO

import click
import typer
from typer.core import TyperArgument, TyperCommand, TyperGroup, TyperOption

from toolang.base.types.message import (
    AudioPart,
    DocumentPart,
    ImagePart,
    Percept,
    PerceptPart,
    TextPart,
    message_text,
    parts_to_data,
)
from toolang.common.errors import ToolangError
from toolang.common.ids import IdIssuer
from toolang.common.layout import AgentLayout
from toolang.execution.executor import CeilingSpec, RunExecutor, RunHandle, RunSpec
from toolang.execution.records import RunRecord
from toolang.execution.store import RunStore
from toolang.execution.threads import ThreadManager
from toolang.execution.types import ThreadPrefix
from toolang.lang.ast import AgicDecl, FlowDecl, Parameter, Program, StructDecl
from toolang.lang.input import coerce_input, perceive_input
from toolang.plugin.models.resolution import split_model_selectors
from toolang.plugin.tools.registry import split_tool_selectors
from toolang.setup import SetupWatcher
from toolang.state.prepare import prepare_agent_state
from toolang.state.state import AgentState, split_cap_selectors
from toolang.up import process as agents
from toolang.up.logging import configure_logging_plan, resolve_agent_logging

from ...common.progress import as_progress_sink, make_cli_progress
from ...common.script_progress import ConsoleRunTracer
from ...common.version import toolang_version

Runnable = AgicDecl | FlowDecl
_LITERAL_ITEM_PREFIX = "\ue002"

_TEXT_MEDIA_TYPES = frozenset(
    {
        "application/javascript",
        "application/json",
        "application/toml",
        "application/xml",
        "application/yaml",
        "application/x-ndjson",
        "application/x-sh",
        "application/x-yaml",
    }
)
_DOCUMENT_EXTENSIONS = frozenset(
    {
        ".csv",
        ".doc",
        ".docx",
        ".html",
        ".json",
        ".md",
        ".pdf",
        ".ppt",
        ".pptx",
        ".rtf",
        ".txt",
        ".xls",
        ".xlsx",
        ".xml",
    }
)


class _HelpArgument(TyperArgument):
    """One signature argument displayed by Typer but parsed by the collector."""

    def add_to_parser(self, parser: Any, ctx: click.Context) -> None:
        del parser, ctx

    def handle_parse_result(
        self,
        ctx: click.Context,
        opts: Mapping[str, Any],
        args: list[str],
    ) -> tuple[None, list[str]]:
        del ctx, opts
        return None, args


class _CollectorArgument(TyperArgument):
    """The hidden variadic parser behind the signature help arguments."""

    def get_usage_pieces(self, ctx: click.Context) -> list[str]:
        del ctx
        return []


class ScriptHelpCommand(TyperCommand):
    """Render generic path-based script usage for the hidden help command."""

    def format_usage(
        self,
        ctx: click.Context,
        formatter: click.HelpFormatter,
    ) -> None:
        command_path = (
            ctx.parent.command_path
            if ctx.parent is not None
            else ctx.command_path
        )
        formatter.write_usage(
            command_path,
            "SCRIPT [RUNNABLE [ARGUMENTS]...]",
        )


def script_command(ctx: typer.Context) -> None:
    """Show how to run an agic or flow from a local Toolang script."""

    typer.echo(ctx.get_help())


def dispatch(
    global_args: list[str],
    argv: list[str],
    *,
    prog_name: str,
    stdin: TextIO | None = None,
) -> int:
    """Dispatch the path shorthand or hidden script command."""

    if global_args:
        typer.echo(
            "toolang error: too <path>.too does not support global CLI options",
            err=True,
        )
        return 1
    if not argv:
        typer.echo("toolang error: missing script path", err=True)
        return 1
    source_path = _source_path(argv[0])
    if source_path is None:
        typer.echo(f"toolang error: script not found: {argv[0]}", err=True)
        return 1
    try:
        program = Program.from_source(source_path.read_text(encoding="utf-8"))
        command = _program_command(
            program,
            source_path=source_path,
            source_label=argv[0],
            stdin=stdin or sys.stdin,
        )
        result = command.main(
            args=_protect_literal_items(argv[1:] or ["--help"]),
            prog_name=f"{prog_name} {argv[0]}",
            standalone_mode=False,
        )
        return int(result) if isinstance(result, int) else 0
    except click.exceptions.Exit as exc:
        return exc.exit_code
    except click.ClickException as exc:
        exc.show(file=sys.stderr)
        return exc.exit_code
    except (OSError, UnicodeError, ValueError, ToolangError) as exc:
        _error(str(exc))
        return 1


def _program_command(
    program: Program,
    *,
    source_path: Path,
    source_label: str,
    stdin: TextIO,
) -> TyperGroup:
    group = TyperGroup(
        name=source_label,
        help=f"Run an agic or flow from {source_label}.",
        no_args_is_help=True,
        rich_markup_mode="rich",
    )
    for runnable in _public_runnables(program):
        group.add_command(
            _runnable_command(
                runnable,
                program=program,
                source_path=source_path,
                stdin=stdin,
            )
        )
    return group


def _runnable_command(
    runnable: Runnable,
    *,
    program: Program,
    source_path: Path,
    stdin: TextIO,
) -> TyperCommand:
    def callback(
        items: tuple[str, ...],
        model: str | None,
        models: tuple[str, ...],
        tools: tuple[str, ...],
        caps: tuple[str, ...],
        quiet: bool,
        verbose: int,
    ) -> int:
        input_value, args = _bind_arguments(
            runnable,
            items=items,
            program=program,
            stdin=stdin,
        )
        return _run(
            source_path,
            runnable=runnable.name,
            runnable_kind=runnable.kind,
            runnable_doc=runnable.doc,
            input_value=input_value,
            args=args,
            model=model,
            ceiling=CeilingSpec(
                models=tuple(dict.fromkeys(split_model_selectors(models))) or None,
                tools=(
                    tuple(dict.fromkeys(split_tool_selectors(tools))) if tools else None
                ),
                caps=tuple(dict.fromkeys(split_cap_selectors(caps))) or None,
            ),
            quiet=quiet,
            verbosity=verbose,
        )

    help_text = runnable.doc.strip() if runnable.doc else None
    params: list[click.Parameter] = [
        TyperOption(
            param_decls=["--model"],
            type=str,
            default=None,
            help="Use this model selector for the run.",
        ),
        TyperOption(
            param_decls=["--models"],
            type=str,
            multiple=True,
            default=(),
            help="Limit available models. Pass CSV or repeat.",
        ),
        TyperOption(
            param_decls=["--tools"],
            type=str,
            multiple=True,
            default=(),
            help="Limit available tools. Pass CSV or repeat.",
        ),
        TyperOption(
            param_decls=["--caps"],
            type=str,
            multiple=True,
            default=(),
            help="Limit available caps. Pass CSV or repeat.",
        ),
        TyperOption(
            param_decls=["--quiet", "-q"],
            is_flag=True,
            default=False,
            help="Suppress execution progress.",
        ),
        TyperOption(
            param_decls=["--verbose", "-v"],
            count=True,
            default=0,
            help="Show more execution progress.",
        ),
        *(_signature_argument(parameter) for parameter in runnable.params),
    ]
    if runnable.input is not None:
        params.append(_input_argument(runnable.input))
    params.append(
        _CollectorArgument(
            param_decls=["items"],
            type=str,
            nargs=-1,
            required=False,
            default=(),
            expose_value=True,
            hidden=True,
        )
    )
    return TyperCommand(
        name=runnable.name,
        callback=callback,
        params=params,
        help=help_text,
        short_help=None,
        rich_markup_mode="rich",
    )


def _signature_argument(parameter: Parameter) -> TyperArgument:
    return _HelpArgument(
        param_decls=[parameter.name],
        type=str,
        required=not parameter.optional,
        metavar=f"{parameter.name}={parameter.type_name or 'Part[]'}",
        help=None,
        expose_value=False,
    )


def _input_argument(parameter: Parameter) -> TyperArgument:
    type_name = parameter.type_name or "Part[]"
    return _HelpArgument(
        param_decls=["input"],
        type=str,
        required=not parameter.optional,
        metavar="INPUT...",
        help=f"Primary {type_name} input. Repeat content items or omit to read stdin.",
        expose_value=False,
    )


def _public_runnables(program: Program) -> tuple[Runnable, ...]:
    return tuple(
        runnable
        for runnable in (*program.agics, *program.flows)
        if runnable.name != "default" and not runnable.name.startswith("<")
    )


def _bind_arguments(
    runnable: Runnable,
    *,
    items: tuple[str, ...],
    program: Program,
    stdin: TextIO,
) -> tuple[Percept, dict[str, object]]:
    params = {parameter.name: parameter for parameter in runnable.params}
    raw_args: dict[str, str] = {}
    input_items: list[str] = []
    for item in items:
        if item.startswith(_LITERAL_ITEM_PREFIX):
            input_items.append(item.removeprefix(_LITERAL_ITEM_PREFIX))
            continue
        name, separator, value = item.partition("=")
        if separator and name in params:
            if name in raw_args:
                raise click.BadParameter(
                    f"argument {name} was provided more than once"
                )
            raw_args[name] = value
            continue
        input_items.append(item)

    missing = [
        parameter.name
        for parameter in runnable.params
        if not parameter.optional and parameter.name not in raw_args
    ]
    if missing:
        joined = ", ".join(f"{name}=..." for name in missing)
        raise click.UsageError(f"missing required arguments: {joined}")

    include = _include_resolver(Path.cwd())
    structs = {struct.name: struct for struct in program.structs}
    args = {
        name: _coerce_argument(
            value,
            parameter=params[name],
            program=program,
            structs=structs,
            include=include,
        )
        for name, value in raw_args.items()
    }
    input_source = _input_source(input_items, stdin=stdin)
    if runnable.input is None:
        if input_source is not None:
            raise click.UsageError(f"{runnable.name} does not accept primary input")
        return (), args
    if input_source is None:
        if not runnable.input.optional:
            raise click.UsageError(f"{runnable.name} requires primary input")
        return (), args
    percept = perceive_input(
        input_source,
        program=program,
        include=include,
    )
    coerce_input(
        percept,
        runnable.input.type_name or "Part[]",
        structs=structs,
    )
    return percept, args


def _coerce_argument(
    source: str,
    *,
    parameter: Parameter,
    program: Program,
    structs: dict[str, StructDecl],
    include: Callable[[str], PerceptPart],
) -> object:
    percept = perceive_input(
        source,
        program=program,
        include=include,
    )
    return coerce_input(
        percept,
        parameter.type_name or "Part[]",
        structs=structs,
    )


def _input_source(items: list[str], *, stdin: TextIO) -> str | None:
    if items == ["-"]:
        value = stdin.read()
        return value if value else None
    if "-" in items:
        raise click.UsageError("stdin marker '-' must be the only primary input")
    if items:
        lines: list[str] = []
        words: list[str] = []

        def flush_words() -> None:
            if words:
                lines.append(" ".join(words))
                words.clear()

        for item in items:
            if item.startswith("@") or "\n" in item:
                flush_words()
                lines.append(item)
            else:
                words.append(item)
        flush_words()
        return "\n".join(lines)
    if not stdin.isatty():
        value = stdin.read()
        return value if value else None
    return None


def _protect_literal_items(argv: list[str]) -> list[str]:
    try:
        separator = argv.index("--")
    except ValueError:
        return argv
    return [
        *argv[: separator + 1],
        *(
            f"{_LITERAL_ITEM_PREFIX}{item}"
            for item in argv[separator + 1 :]
        ),
    ]


def _include_resolver(base: Path) -> Callable[[str], PerceptPart]:
    def resolve(reference: str) -> PerceptPart:
        path = Path(reference).expanduser()
        if not path.is_absolute():
            path = base / path
        path = path.resolve()
        if not path.is_file():
            raise click.BadParameter(f"included file not found: {reference}")
        media_type, _encoding = mimetypes.guess_type(path.name)
        if _is_text(media_type):
            try:
                return TextPart(path.read_text(encoding="utf-8"))
            except UnicodeDecodeError as exc:
                raise click.BadParameter(
                    f"included text is not UTF-8: {reference}"
                ) from exc
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        if media_type is not None and media_type.startswith("image/"):
            return ImagePart(
                image_url=_data_url(media_type, encoded),
                filename=path.name,
                media_type=media_type,
            )
        if media_type in {"audio/mpeg", "audio/mp3"}:
            return AudioPart(
                data=encoded,
                format="mp3",
                filename=path.name,
                media_type=media_type,
            )
        if media_type in {"audio/wav", "audio/x-wav"}:
            return AudioPart(
                data=encoded,
                format="wav",
                filename=path.name,
                media_type=media_type,
            )
        if path.suffix.lower() in _DOCUMENT_EXTENSIONS:
            return DocumentPart(
                data=_data_url(media_type or "application/octet-stream", encoded),
                filename=path.name,
                media_type=media_type,
            )
        raise click.BadParameter(f"unsupported included file: {reference}")

    return resolve


def _is_text(media_type: str | None) -> bool:
    return bool(
        media_type
        and (
            media_type.startswith("text/")
            or media_type in _TEXT_MEDIA_TYPES
        )
    )


def _data_url(media_type: str, encoded: str) -> str:
    return f"data:{media_type};base64,{encoded}"


def _run(
    source_path: Path,
    *,
    runnable: str,
    runnable_kind: str,
    runnable_doc: str | None,
    input_value: Percept,
    args: dict[str, object],
    model: str | None,
    ceiling: CeilingSpec,
    quiet: bool,
    verbosity: int,
) -> int:
    progress = make_cli_progress() if not quiet and sys.stderr.isatty() else None
    layout: AgentLayout | None = None
    store: RunStore | None = None
    run_id: str | None = None
    log_path: Path | None = None
    try:
        layout = agents.materialize_roaming_program(source_path)
        store = RunStore(layout.run_store)
        ids = IdIssuer(layout.id_state)
        run_id = ids.issue_run()
        log_plan = resolve_agent_logging(
            mode="script",
            environ=os.environ,
            run_log_path=layout.run_log(runnable, run_id),
        )
        configure_logging_plan(log_plan)
        log_path = log_plan.path
        state = prepare_agent_state(
            layout,
            toolang_version=toolang_version(),
            progress=as_progress_sink(progress),
        )
        if progress is not None:
            progress.finish(details=False)
        result = asyncio.run(
            _execute(
                layout=layout,
                state=state,
                store=store,
                ids=ids,
                run_id=run_id,
                runnable=runnable,
                runnable_kind=runnable_kind,
                runnable_doc=runnable_doc,
                input_value=input_value,
                args=args,
                model=model,
                ceiling=ceiling,
                quiet=quiet,
                verbosity=verbosity,
            )
        )
    except KeyboardInterrupt:
        if progress is not None:
            progress.interrupt()
        interruption_reported = False
        if (
            store is not None
            and run_id is not None
            and not quiet
            and (sys.stderr.isatty() or verbosity > 0)
        ):
            record = store.get_run(run_id=run_id)
            interruption_reported = (
                record is not None and record.status == "canceled"
            )
        if not interruption_reported:
            typer.echo("toolang interrupted", err=True)
        if run_id is not None:
            typer.echo(f"Run: {run_id}", err=True)
        if log_path is not None and log_path.exists():
            typer.echo(f"Log: {log_path}", err=True)
        return 130
    except (OSError, ValueError, ToolangError, RuntimeError) as exc:
        if progress is not None:
            progress.finish(details=False)
        _error(str(exc))
        if run_id is not None:
            typer.echo(f"Run: {run_id}", err=True)
        if log_path is not None and log_path.exists():
            typer.echo(f"Log: {log_path}", err=True)
        return 1
    finally:
        if store is not None:
            store.close()
    if layout is None:
        raise RuntimeError("script layout was not prepared")
    return _emit_result(
        result,
        store_path=layout.run_store,
        log_path=log_path,
        error_reported=(
            not quiet and (sys.stderr.isatty() or verbosity > 0)
        ),
    )


async def _execute(
    *,
    layout: AgentLayout,
    state: AgentState,
    store: RunStore,
    ids: IdIssuer,
    run_id: str,
    runnable: str,
    runnable_kind: str,
    runnable_doc: str | None,
    input_value: Percept,
    args: dict[str, object],
    model: str | None,
    ceiling: CeilingSpec,
    quiet: bool,
    verbosity: int,
) -> RunRecord:
    setup = await SetupWatcher(layout).refresh()
    thread = ThreadManager(store, ids).create(prefix=ThreadPrefix.SCRIPT)
    executor = RunExecutor(store, ids)
    tracer = (
        ConsoleRunTracer(
            run_id=run_id,
            verbosity=verbosity,
            runnable_kind=runnable_kind,
            runnable_name=runnable,
            runnable_doc=runnable_doc,
            input_value=input_value,
            args=args,
        )
        if not quiet and (sys.stderr.isatty() or verbosity > 0)
        else None
    )
    handle = executor.start(
        RunSpec(
            setup=setup,
            state=state,
            ceiling=ceiling,
            thread=thread,
            runnable=runnable,
            input=input_value,
            model=model,
            args=args or None,
        ),
        run_id=run_id,
        tracer=tracer,
    )
    try:
        return await _await_script_run(handle)
    finally:
        try:
            await executor.shutdown()
        finally:
            if tracer is not None:
                tracer.close()


async def _await_script_run(handle: RunHandle) -> RunRecord:
    """Stop an owned one-shot run when its script caller is interrupted."""

    try:
        return await handle
    except asyncio.CancelledError:
        if not handle.task.done():
            try:
                handle.stop(reason="script interrupted")
            except ValueError:
                record = handle.executor.store.get_run(run_id=handle.run_id)
                if record is None or record.status in {"pending", "running"}:
                    raise
            if not handle.task.done():
                await asyncio.shield(handle.task)
        raise


def _emit_result(
    result: RunRecord,
    *,
    store_path: Path,
    log_path: Path | None,
    error_reported: bool = False,
) -> int:
    if result.status != "finished":
        if not error_reported:
            _error(result.error or f"run {result.status}")
        typer.echo(f"Run: {result.id}", err=True)
        if log_path is not None and log_path.exists():
            typer.echo(f"Log: {log_path}", err=True)
        return 1
    store = RunStore(store_path)
    try:
        output = store.run_output(run_id=result.id)
    finally:
        store.close()
    if not output:
        return 0
    if all(isinstance(part, TextPart) for part in output):
        text = message_text(output)
        if text:
            typer.echo(text, nl=not text.endswith("\n"))
        return 0
    typer.echo(
        json.dumps(
            parts_to_data(output),
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )
    return 0


def _source_path(token: str) -> Path | None:
    text = token.strip()
    if not text or text.startswith("-"):
        return None
    try:
        source = Path(text).expanduser().resolve()
    except OSError:
        return None
    return source if source.is_file() and source.suffix == ".too" else None


def _error(message: str) -> None:
    typer.echo(f"toolang error: {message}", err=True)
