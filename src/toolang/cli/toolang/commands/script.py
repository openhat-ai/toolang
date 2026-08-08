"""Run authored agics and flows from one local Toolang script."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import replace
import json
import os
from pathlib import Path
import sys
from typing import Any, TextIO

import click
import typer
from typer.core import TyperArgument, TyperCommand, TyperGroup, TyperOption

from toolang.base.types.message import (
    TextPart,
    message_text,
    parts_to_data,
)
from toolang.common.errors import ToolangError
from toolang.common.ids import IdIssuer
from toolang.common.layout import AgentLayout
from toolang.cli.common.policy import (
    resolve_binding_overrides,
    resolve_ceiling_overrides,
    resolve_limit_overrides,
)
from toolang.execution.calls import bind_runnable_call
from toolang.execution.executor import RunExecutor, RunHandle
from toolang.execution.records import RunRecord
from toolang.execution.runnables import resolve_runnable
from toolang.execution.store import RunStore
from toolang.execution.threads import ThreadManager
from toolang.execution.types import ThreadPrefix
from toolang.lang.ast import AgicDecl, FlowDecl, Parameter, Program
from toolang.lang.includes import resolve_file_include
from toolang.lang.submission import (
    Arguments,
    RunnableCall,
    parse_runnable_call,
)
from toolang.setup import SetupWatcher
from toolang.state.prepare import prepare_agent_state
from toolang.state.state import AgentState
from toolang.up import process as agents
from toolang.up.logging import configure_logging_plan, resolve_agent_logging

from ...common.context import load_runtime_environ
from ...common.progress import as_progress_sink, make_cli_progress
from ...common.output import echo_error
from ...common.script_progress import ConsoleRunTracer
from ...common.version import toolang_version

Runnable = AgicDecl | FlowDecl
_LITERAL_ITEM_PREFIX = "\ue002"
_UNPERSISTED_THREAD = "<unpersisted-script-thread>"
_RUNNABLES_PANEL = "Runnables"


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


class _IncompleteRunnableCall(Exception):
    """A dynamic runnable command is missing required call input."""


class _RunnableCommand(TyperCommand):
    """Show runnable help when its collected call is incomplete."""

    def invoke(self, ctx: click.Context) -> Any:
        try:
            return TyperCommand.invoke(self, ctx)
        except _IncompleteRunnableCall:
            click.echo(ctx.get_help())
            ctx.exit(2)


def dispatch(
    global_args: list[str],
    argv: list[str],
    *,
    prog_name: str,
    stdin: TextIO | None = None,
) -> int:
    """Dispatch one path-based runnable invocation."""

    if global_args:
        echo_error("too <path>.too does not support global CLI options")
        return 1
    if not argv:
        echo_error("missing script path")
        return 1
    source_path = _source_path(argv[0])
    if source_path is None:
        echo_error(f"script not found: {argv[0]}")
        return 1
    try:
        program = Program.from_source(source_path.read_text(encoding="utf-8"))
        command = _program_command(
            program,
            source_path=source_path,
            source_label=argv[0],
            stdin=stdin or sys.stdin,
        )
        command_args = _typed_runnable_args(program, argv[1:])
        result = command.main(
            args=_protect_literal_items(command_args or ["--help"]),
            prog_name=f"{prog_name} {argv[0]}",
            standalone_mode=False,
        )
        return int(result) if isinstance(result, int) else 0
    except click.exceptions.Exit as exc:
        return exc.exit_code
    except click.ClickException as exc:
        echo_error(exc)
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
        subcommand_metavar="RUNNABLE [ARGS]...",
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
        allow: tuple[str, ...],
        default: tuple[str, ...],
        limit: tuple[str, ...],
        quiet: bool,
        verbose: int,
    ) -> int:
        call, raw_args = _collect_call(
            runnable,
            items=items,
            stdin=stdin,
        )
        return _run(
            source_path,
            runnable=runnable.name,
            call=call,
            raw_args=raw_args,
            allow_options=allow,
            default_options=default,
            limit_options=limit,
            quiet=quiet,
            verbosity=verbose,
        )

    help_text = runnable.doc.strip() if runnable.doc else None
    params: list[click.Parameter] = [
        TyperOption(
            param_decls=["--allow"],
            type=str,
            multiple=True,
            default=(),
            help="Set DOMAIN=SELECTORS. Repeat by domain.",
        ),
        TyperOption(
            param_decls=["--default"],
            type=str,
            multiple=True,
            default=(),
            help="Set FIELD=VALUE. Repeat for another field.",
        ),
        TyperOption(
            param_decls=["--limit"],
            type=str,
            multiple=True,
            default=(),
            help="Set FIELD=VALUE. Repeat for another field.",
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
    return _RunnableCommand(
        name=runnable.name,
        callback=callback,
        params=params,
        help=help_text,
        short_help=None,
        rich_help_panel=_RUNNABLES_PANEL,
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


def _typed_runnable_args(program: Program, args: list[str]) -> list[str]:
    """Remove one explicit runnable-kind prefix and validate its declaration."""

    if not args:
        return args
    kind, separator, name = args[0].partition(":")
    if not separator or kind not in {"agic", "flow", "runnable"}:
        return args
    name = name.strip()
    if not name:
        raise ValueError(f"{kind} selector cannot be empty")
    matches = tuple(
        runnable for runnable in _public_runnables(program) if runnable.name == name
    )
    if not matches:
        raise ValueError(f"runnable not found: {name}")
    runnable = matches[0]
    if kind == "agic" and not isinstance(runnable, AgicDecl):
        raise ValueError(f"runnable is not an agic: {name}")
    if kind == "flow" and not isinstance(runnable, FlowDecl):
        raise ValueError(f"runnable is not a flow: {name}")
    return [name, *args[1:]]


def _collect_call(
    runnable: Runnable,
    *,
    items: tuple[str, ...],
    stdin: TextIO,
) -> tuple[RunnableCall, Arguments]:
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

    input_source = _input_source(input_items, stdin=stdin)
    call = (
        RunnableCall(overrides=(), content="")
        if input_source is None
        else parse_runnable_call(input_source)
    )
    has_runnable_override = any(
        item.kind in {"agic", "flow"} for item in call.overrides
    )
    if not has_runnable_override:
        missing = [
            parameter.name
            for parameter in runnable.params
            if not parameter.optional and parameter.name not in raw_args
        ]
        if missing:
            raise _IncompleteRunnableCall
        if runnable.input is not None and not runnable.input.optional and not call.content:
            raise _IncompleteRunnableCall
    return call, tuple(raw_args.items())


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


def _run(
    source_path: Path,
    *,
    runnable: str,
    call: RunnableCall,
    raw_args: Arguments,
    allow_options: tuple[str, ...],
    default_options: tuple[str, ...],
    limit_options: tuple[str, ...],
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
                call=call,
                raw_args=raw_args,
                allow_options=allow_options,
                default_options=default_options,
                limit_options=limit_options,
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
    call: RunnableCall,
    raw_args: Arguments,
    allow_options: tuple[str, ...],
    default_options: tuple[str, ...],
    quiet: bool,
    verbosity: int,
    limit_options: tuple[str, ...] = (),
) -> RunRecord:
    environ = load_runtime_environ(layout, base_environ=os.environ)
    cli_bindings = resolve_binding_overrides({}, default_options)
    if "runnable" in cli_bindings:
        raise ValueError(
            "--default runnable does not apply when a script runnable is explicit"
        )
    setup = await SetupWatcher(
        layout,
        ceiling_overrides=resolve_ceiling_overrides(environ, allow_options),
        binding_overrides={
            **resolve_binding_overrides(environ),
            **cli_bindings,
        },
        limit_overrides=resolve_limit_overrides(environ, limit_options),
    ).refresh()
    executor = RunExecutor(store, ids)
    spec = bind_runnable_call(
        call,
        setup=setup,
        state=state,
        thread=_UNPERSISTED_THREAD,
        default_runnable=runnable,
        selected_runnable=runnable,
        default_raw_args=raw_args,
        include=lambda reference: resolve_file_include(
            reference,
            base=Path.cwd(),
        ),
    )
    executor.validate(spec)
    thread = ThreadManager(store, ids).create(prefix=ThreadPrefix.SCRIPT)
    spec = replace(spec, thread=thread)
    selected = resolve_runnable(state.program, spec.runnable)
    tracer = (
        ConsoleRunTracer(
            run_id=run_id,
            verbosity=verbosity,
            runnable_kind=selected.kind,
            runnable_name=selected.name,
            runnable_doc=selected.doc,
            input_value=spec.input,
            args=dict(spec.args or {}),
        )
        if not quiet and (sys.stderr.isatty() or verbosity > 0)
        else None
    )
    handle = executor.start(
        spec,
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
    echo_error(message)
