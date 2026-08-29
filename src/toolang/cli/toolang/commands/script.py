"""Run authored agics and flows from one local Toolang script."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace
import json
import os
from pathlib import Path
import sys
from typing import Any, TextIO
from uuid import uuid4

import click
import httpx
from pydantic import TypeAdapter, ValidationError
import typer
from typer.core import TyperArgument, TyperCommand, TyperGroup, TyperOption

from toolang.base.types.policy import RunBindings
from toolang.common.errors import ToolangError
from toolang.common.ids import IdIssuer
from toolang.common.layout import AgentLayout
from toolang.cli.common.policy import (
    resolve_binding_overrides,
    resolve_ceiling_overrides,
    resolve_limit_overrides,
)
from toolang.execution.calls import parse_call, resolve_spec
from toolang.execution.executor import LocalRunHandle, RunExecutor
from toolang.execution.remote import RemoteRunClient, RemoteRunClientError
from toolang.execution.records import RunRecord
from toolang.execution.schemas import RunRequest, ThreadInfo
from toolang.execution.store import RunStore
from toolang.execution.threads import ThreadManager
from toolang.execution.types import RunOverride, ThreadPrefix
from toolang.lang.ast import AgicDecl, FlowDecl, Parameter, Program
from toolang.lang.includes import resolve_file_include
from toolang.lang.input import NamedInputSources, RunnableInputRaw
from toolang.setup import SetupWatcher
from toolang.state.prepare import prepare_agent_state
from toolang.state.state import AgentState
from toolang.state.watcher import StateWatcher
from toolang.up import process as agents
from toolang.up.logging import configure_logging_plan, resolve_agent_logging

from ...common.context import load_runtime_environ
from ...common.agent_server import DEVELOPMENT_WHEEL_HELP, acquire_agent_server
from ...common.progress import make_cli_progress
from ...common.remote_runtime import inspect_remote_runtime
from ...common.result_saving import save_result
from ...common.output import echo_error
from ...common.execution_progress.config import resolve_progress_max_width
from ...common.script_progress import ScriptRunPresenter

Runnable = AgicDecl | FlowDecl
_LITERAL_ITEM_PREFIX = "\ue002"
_UNPERSISTED_THREAD = "<unpersisted-script-thread>"
_RUNNABLES_PANEL = "Runnables"
_THREAD_INFO_ADAPTER = TypeAdapter(ThreadInfo)


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


class _IncompleteRunnableInput(Exception):
    """A dynamic runnable command is missing required input."""


class _RunnableCommand(TyperCommand):
    """Show runnable help when its collected call is incomplete."""

    def invoke(self, ctx: click.Context) -> Any:
        try:
            return TyperCommand.invoke(self, ctx)
        except _IncompleteRunnableInput:
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
        sandbox: str | None,
        dev: Path | None,
        save: str | None,
        quiet: bool,
    ) -> int:
        commands, input, raw_named = _collect_call(
            runnable,
            items=items,
            stdin=stdin,
        )
        return _run(
            source_path,
            runnable=runnable.name,
            commands=commands,
            input=input,
            raw_named=raw_named,
            allow_options=allow,
            default_options=default,
            limit_options=limit,
            sandbox=sandbox,
            dev=dev,
            save=save,
            quiet=quiet,
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
            param_decls=["--limit"],
            type=str,
            multiple=True,
            default=(),
            help="Set FIELD=VALUE. Repeat for another field.",
        ),
        TyperOption(
            param_decls=["--default"],
            type=str,
            multiple=True,
            default=(),
            help="Set FIELD=VALUE. Repeat for another field.",
        ),
        TyperOption(
            param_decls=["--sandbox"],
            type=str,
            default=None,
            help="Execute this run in the selected sandbox.",
        ),
        TyperOption(
            param_decls=["--dev"],
            type=click.Path(path_type=Path),
            default=None,
            metavar="PATH",
            help=DEVELOPMENT_WHEEL_HELP,
        ),
        TyperOption(
            param_decls=["--save"],
            type=str,
            default=None,
            metavar="DEST",
            help="Save the Run result to PATH, or use - for stdout.",
        ),
        TyperOption(
            param_decls=["--quiet", "-q"],
            is_flag=True,
            default=False,
            help="Suppress prepare and execution progress.",
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
) -> tuple[tuple[RunOverride, ...], RunnableInputRaw, NamedInputSources]:
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
                raise click.BadParameter(f"argument {name} was provided more than once")
            raw_args[name] = value
            continue
        input_items.append(item)

    input_source = _input_source(input_items, stdin=stdin)
    commands, input = parse_call(input_source or "")
    has_runnable_override = any(
        command.group == "default" and command.field == "runnable"
        for command in commands
    )
    if not has_runnable_override:
        missing = [
            parameter.name
            for parameter in runnable.params
            if not parameter.optional and parameter.name not in raw_args
        ]
        if missing:
            raise _IncompleteRunnableInput
        if (
            runnable.input is not None
            and not runnable.input.optional
            and input.primary is None
        ):
            raise _IncompleteRunnableInput
    return commands, input, tuple(raw_args.items())


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
        *(f"{_LITERAL_ITEM_PREFIX}{item}" for item in argv[separator + 1 :]),
    ]


def _run(
    source_path: Path,
    *,
    runnable: str,
    commands: tuple[RunOverride, ...],
    input: RunnableInputRaw,
    raw_named: NamedInputSources,
    allow_options: tuple[str, ...],
    default_options: tuple[str, ...],
    limit_options: tuple[str, ...],
    sandbox: str | None,
    dev: Path | None,
    save: str | None,
    quiet: bool,
) -> int:
    progress = make_cli_progress(enabled=not quiet)
    layout: AgentLayout | None = None
    store: RunStore | None = None
    run_id: str | None = None
    log_path: Path | None = None
    accepted: list[str] = []
    try:
        layout = agents.materialize_roaming_program(source_path)
        _reject_runnable_option(default_options)
        with acquire_agent_server(
            layout,
            sandbox=sandbox,
            dev=dev,
            show_progress=not quiet,
        ) as server:
            if server is None:
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
                with progress:
                    state = prepare_agent_state(
                        layout,
                        progress=progress.sink,
                    )
                result = asyncio.run(
                    _execute(
                        layout=layout,
                        state=state,
                        store=store,
                        ids=ids,
                        run_id=run_id,
                        sandbox="host",
                        runnable=runnable,
                        commands=commands,
                        input=input,
                        raw_named=raw_named,
                        allow_options=allow_options,
                        default_options=default_options,
                        limit_options=limit_options,
                        quiet=quiet,
                    )
                )
            else:
                log_path = layout.runtime_log
                result = asyncio.run(
                    _execute_remote(
                        layout=layout,
                        endpoint=server.endpoint,
                        sandbox=server.sandbox,
                        runnable=runnable,
                        commands=commands,
                        input=input,
                        raw_named=raw_named,
                        allow_options=allow_options,
                        default_options=default_options,
                        limit_options=limit_options,
                        quiet=quiet,
                        on_accept=accepted.append,
                    )
                )
                run_id = accepted[0] if accepted else None
    except KeyboardInterrupt:
        if run_id is None and accepted:
            run_id = accepted[0]
        progress.close()
        interruption_reported = False
        if layout is not None and run_id is not None and not quiet:
            record = _stored_run(layout, run_id, store=store)
            interruption_reported = record is not None and record.status == "canceled"
        if not interruption_reported:
            typer.echo("toolang interrupted", err=True)
        if log_path is not None and log_path.exists():
            typer.echo(f"Log: {log_path}", err=True)
        return 130
    except (OSError, ValueError, ToolangError, RuntimeError) as exc:
        progress.close()
        _error(str(exc))
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
        save=save,
        error_reported=not quiet,
    )


def _reject_runnable_option(default_options: tuple[str, ...]) -> None:
    if "runnable" in resolve_binding_overrides({}, default_options):
        raise ValueError(
            "--default runnable does not apply when a script runnable is explicit"
        )


async def _execute_remote(
    *,
    layout: AgentLayout,
    endpoint: str,
    sandbox: str,
    runnable: str,
    commands: tuple[RunOverride, ...],
    input: RunnableInputRaw,
    raw_named: NamedInputSources,
    allow_options: tuple[str, ...],
    default_options: tuple[str, ...],
    limit_options: tuple[str, ...],
    quiet: bool,
    on_accept: Callable[[str], None] | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> RunRecord:
    """Execute one script request through a validated AgentServer."""

    environ = load_runtime_environ(layout, base_environ=os.environ)
    request_input = _remote_script_input(input, raw_named=raw_named)
    session_commands = _remote_script_session_commands(
        runnable=runnable,
        allow_options=allow_options,
        default_options=default_options,
        limit_options=limit_options,
    )
    tracer = (
        ScriptRunPresenter(
            run_id=None,
            max_width=resolve_progress_max_width(environ),
        )
        if not quiet
        else None
    )
    async with httpx.AsyncClient(
        transport=transport,
        timeout=httpx.Timeout(3.0),
    ) as http:
        client = RemoteRunClient(endpoint, client=http)
        try:
            await client.connect()
            await inspect_remote_runtime(
                http,
                client.endpoint,
                expected_sandbox=sandbox,
            )
            thread = await _create_remote_script_thread(http, client.endpoint)
            handle = await client.run(
                RunRequest(
                    thread=thread,
                    commands=_remote_script_commands(commands, runnable=runnable),
                    input=request_input,
                    session_commands=session_commands,
                    runnable_fallbacks=(runnable,),
                    request_id=f"term_{uuid4().hex}",
                ),
                tracer=tracer,
            )
            if on_accept is not None:
                on_accept(handle.run_id)
            try:
                detail = await handle.wait()
            except BaseException as exc:
                await _cancel_remote_script_run(
                    client,
                    handle.run_id,
                    handle.wait,
                    reason=(
                        "script interrupted"
                        if isinstance(exc, asyncio.CancelledError | KeyboardInterrupt)
                        else "script client failed"
                    ),
                )
                raise
            record = _stored_run(layout, detail.id)
            if record is None:
                raise RuntimeError(
                    f"run detail missing from the script store: {detail.id}"
                )
            return record
        finally:
            await client.disconnect()
            if tracer is not None:
                tracer.close()


def _remote_script_input(
    input: RunnableInputRaw,
    *,
    raw_named: NamedInputSources,
) -> RunnableInputRaw:
    """Encode CLI-surface named sources in the authored request input."""

    if input.named and raw_named:
        raise ValueError("named inputs cannot be supplied by both source and surface")
    return replace(input, named=input.named or raw_named)


def _remote_script_commands(
    commands: tuple[RunOverride, ...],
    *,
    runnable: str,
) -> tuple[RunOverride, ...]:
    """Keep `:runnable default` anchored to the dynamic CLI runnable."""

    return tuple(
        RunOverride("default", "runnable", runnable)
        if command.group == "default"
        and command.field == "runnable"
        and command.value is None
        else command
        for command in commands
    )


def _remote_script_session_commands(
    *,
    runnable: str,
    allow_options: tuple[str, ...],
    default_options: tuple[str, ...],
    limit_options: tuple[str, ...],
) -> tuple[RunOverride, ...]:
    """Place the explicit CLI runnable above AgentServer setup bindings."""

    ceilings = resolve_ceiling_overrides({}, allow_options)
    bindings = resolve_binding_overrides({}, default_options)
    limits = resolve_limit_overrides({}, limit_options)
    if "runnable" in bindings:
        raise ValueError(
            "--default runnable does not apply when a script runnable is explicit"
        )
    return (
        RunOverride("default", "runnable", runnable),
        *(RunOverride("allow", field, value) for field, value in ceilings.items()),
        *(RunOverride("default", field, value) for field, value in bindings.items()),
        *(RunOverride("limit", field, value) for field, value in limits.items()),
    )


async def _create_remote_script_thread(
    client: httpx.AsyncClient,
    endpoint: str,
) -> str:
    try:
        response = await client.post(
            f"{endpoint}/api/v1/threads",
            json={"client": "script"},
        )
    except (httpx.HTTPError, RuntimeError) as exc:
        raise RuntimeError(
            f"remote script thread creation failed: {type(exc).__name__}"
        ) from exc
    if not response.is_success:
        detail = response.reason_phrase or "request failed"
        try:
            payload = response.json()
        except (UnicodeDecodeError, json.JSONDecodeError):
            pass
        else:
            if isinstance(payload, Mapping) and isinstance(payload.get("detail"), str):
                detail = str(payload["detail"])
        raise RuntimeError(
            "remote script thread creation failed: "
            f"HTTP {response.status_code} {detail}"
        )
    try:
        payload = response.json()
        if not isinstance(payload, Mapping) or set(payload) != {"thread"}:
            raise ValueError
        thread = _THREAD_INFO_ADAPTER.validate_python(payload["thread"])
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValidationError,
        ValueError,
    ) as exc:
        raise RuntimeError(
            "remote script thread creation returned invalid data"
        ) from exc
    if thread.origin != "script" or not thread.id.startswith("script_"):
        raise RuntimeError("remote script thread creation returned invalid identity")
    return thread.id


async def _cancel_remote_script_run(
    client: RemoteRunClient,
    run_id: str,
    wait: Callable[[], Awaitable[object]],
    *,
    reason: str,
) -> None:
    try:
        await client.cancel(
            run_id,
            request_id=f"term_{uuid4().hex}",
            reason=reason,
        )
    except (OSError, ValueError, RemoteRunClientError, RuntimeError):
        return
    try:
        await asyncio.wait_for(asyncio.shield(wait()), timeout=5)
    except (TimeoutError, OSError, ValueError, RemoteRunClientError, RuntimeError):
        pass


def _stored_run(
    layout: AgentLayout,
    run_id: str,
    *,
    store: RunStore | None = None,
) -> RunRecord | None:
    if store is not None:
        return store.get_run(run_id=run_id)
    opened = RunStore(layout.run_store)
    try:
        return opened.get_run(run_id=run_id)
    finally:
        opened.close()


async def _execute(
    *,
    layout: AgentLayout,
    state: AgentState,
    store: RunStore,
    ids: IdIssuer,
    run_id: str,
    sandbox: str,
    runnable: str,
    commands: tuple[RunOverride, ...],
    input: RunnableInputRaw,
    raw_named: NamedInputSources,
    allow_options: tuple[str, ...],
    default_options: tuple[str, ...],
    quiet: bool,
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
        sandbox=sandbox,
        ceiling_overrides=resolve_ceiling_overrides(environ, allow_options),
        binding_overrides={
            **resolve_binding_overrides(environ),
            **cli_bindings,
        },
        limit_overrides=resolve_limit_overrides(environ, limit_options),
    ).refresh()
    state_watcher = StateWatcher(layout)
    await state_watcher.refresh()
    executor = RunExecutor(
        store,
        ids,
        refresh_state=state_watcher.refresh_result,
    )
    spec = resolve_spec(
        commands,
        input,
        setup=setup,
        state=state,
        thread=_UNPERSISTED_THREAD,
        default_runnable=runnable,
        surface=RunBindings(runnable=runnable),
        surface_named_sources=raw_named,
        include=lambda reference: resolve_file_include(
            reference,
            base=Path.cwd(),
        ),
    )
    executor.validate(spec)
    thread = ThreadManager(store, ids).create(prefix=ThreadPrefix.SCRIPT)
    spec = replace(spec, thread=thread)
    if spec.bindings.runnable is None:
        raise RuntimeError("resolved script spec has no runnable binding")
    tracer = (
        ScriptRunPresenter(
            run_id=run_id,
            max_width=resolve_progress_max_width(environ),
        )
        if not quiet
        else None
    )
    executor.start()
    try:
        handle = executor.run(
            spec,
            run_id=run_id,
            tracer=tracer,
        )
        return await _await_script_run(handle)
    finally:
        try:
            await executor.stop()
        finally:
            if tracer is not None:
                tracer.close()


async def _await_script_run(handle: LocalRunHandle) -> RunRecord:
    """Cancel an owned one-shot run when its script caller is interrupted."""

    try:
        return await handle
    except asyncio.CancelledError:
        if not handle.task.done():
            try:
                handle.cancel(reason="script interrupted")
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
    save: str | None = None,
    error_reported: bool = False,
) -> int:
    if result.status != "succeeded":
        store = RunStore(store_path)
        try:
            if not error_reported:
                _error(
                    (
                        store.resolve_error(result.error)
                        if result.error is not None
                        else None
                    )
                    or f"run {result.status}"
                )
        finally:
            store.close()
        if log_path is not None and log_path.exists():
            typer.echo(f"Log: {log_path}", err=True)
        return 1
    if save is None:
        return 0

    store = RunStore(store_path)
    try:
        output = store.run_output(run_id=result.id)
    finally:
        store.close()
    try:
        save_result(output, save, stdout=sys.stdout)
    except (OSError, TypeError, UnicodeError, ValueError) as exc:
        _error(str(exc))
        return 1
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
