"""Run authored agics and flows from one local Toolang script."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace
import json
import os
from pathlib import Path
import sys
from typing import Annotated, Any, TextIO, cast
from uuid import uuid4

import httpx
from pydantic import TypeAdapter, ValidationError
from rich.text import Text
import typer
from typer._click import Context, HelpFormatter
from typer._click.core import ParameterSource
from typer._click.exceptions import ClickException, UsageError
from typer._click.parser import _OptionParser, _ParsingState
from typer.core import TyperCommand, TyperGroup, TyperOption
from typer.main import get_command_from_info
from typer.models import CommandInfo

from toolang.base.model_settings import parse_model_body
from toolang.base.types.model import ModelRequest
from toolang.base.types.policy import RunBindings, RunPolicy
from toolang.common.errors import ToolangError
from toolang.common.ids import IdIssuer
from toolang.common.layout import AgentLayout
from toolang.common.typer.ui import HelpFormatter as UIHelpFormatter
from toolang.cli.common.policy import (
    resolve_default_overrides,
    resolve_compact_override,
    resolve_ceiling_overrides,
    resolve_limit_overrides,
)
from toolang.cli.common.model_selection import (
    is_concrete_model_ref,
    materialize_model_selection,
)
from toolang.execution.calls import parse_call, resolve_spec
from toolang.execution.policy import apply_session_setting, materialize_run_setting
from toolang.execution.executor import LocalRunHandle, RunExecutor
from toolang.execution.remote import RemoteRunClient, RemoteRunClientError
from toolang.execution.records import RunRecord
from toolang.execution.schemas import RunRequest, RunnableRequest, ThreadInfo
from toolang.execution.store import RunStore
from toolang.execution.threads import ThreadManager
from toolang.execution.types import (
    AllowField,
    AllowOverride,
    LimitField,
    LimitOverride,
    RunOverride,
    SessionSetting,
    ThreadPrefix,
)
from toolang.lang.ast import (
    AgicDecl,
    FlowDecl,
    FlowStmt,
    Program,
    RepeatStmt,
)
from toolang.lang.description import statement_description
from toolang.lang.includes import resolve_file_include
from toolang.lang.input import CallInput, parse_input
from toolang.state.runnable_collections import runnable_dataset
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
from ...common.help import CliCommand, CliGroup, HelpContext
from ...common.parameters import AllowOptions, LimitOptions
from ...common.runnable_parameters import RunnableArgument, runnable_parameters
from ...common.execution_progress.config import resolve_progress_max_width
from ...common.script_progress import ScriptRunPresenter

Runnable = AgicDecl | FlowDecl
_LINE_INPUT_MARKER = "\ue002"
_UNPERSISTED_THREAD = "<unpersisted-script-thread>"
_THREAD_INFO_ADAPTER = TypeAdapter(ThreadInfo)
_RUN_POLICY_ADAPTER = TypeAdapter(RunPolicy)
_MODEL_REQUEST_ADAPTER = TypeAdapter(ModelRequest)


class _IncompleteRunnableInput(Exception):
    """A dynamic runnable command is missing required input."""


class _RunnableParser(_OptionParser):
    """Parse native options and assignments only until the input boundary."""

    def _process_args_for_options(self, state: _ParsingState) -> None:
        while state.rargs:
            item = state.rargs[0]
            if item == "---":
                raise UsageError(
                    "fenced input marker '---' is not supported in script mode; "
                    "use '-' to read stream input from stdin",
                    ctx=self.ctx,
                )
            if item == "-":
                if len(state.rargs) != 1:
                    raise UsageError(
                        "stdin marker '-' must be the only primary input",
                        ctx=self.ctx,
                    )
                return
            if item == "--":
                state.rargs[0] = _LINE_INPUT_MARKER
                return
            if item.startswith("-") and len(item) > 1:
                self._process_opts(state.rargs.pop(0), state)
                continue
            name, separator, _value = item.partition("=")
            if separator and name.isidentifier():
                state.largs.append(state.rargs.pop(0))
                continue
            state.rargs.insert(0, _LINE_INPUT_MARKER)
            return


class _ScriptHelpFormatter(UIHelpFormatter):
    def write_description(self, ctx: Context) -> None:
        command = ctx.command
        if not isinstance(command, _RunnableCommand):
            return super().write_description(ctx)
        if command.help:
            description = Text.from_markup(command.help)
            if not description.plain.endswith("."):
                description.append(".")
            self.write_text(description)
            self.write_paragraph()

    def write_epilog(self, ctx: Context) -> None:
        super().write_epilog(ctx)
        command = ctx.command
        if isinstance(command, _RunnableCommand) and command._flow is not None:
            self.write_paragraph()
            self.write_text("The flow proceeds as follows:")
            self.write_paragraph()
            for line in _flow_outline(command._flow).split("\n"):
                line.truncate(max(1, self.width - 2), overflow="ellipsis")
                self._write_line(line)

    def _command_rows(self, ctx: Context):
        if not isinstance(ctx.command, TyperGroup):
            return
        for _title, (marker, label, _description) in super()._command_rows(ctx):
            command = ctx.command.get_command(ctx, label.plain)
            if isinstance(command, _RunnableCommand):
                kind = "flow" if command._flow is not None else "agic"
                yield (
                    "Runnables",
                    (
                        marker,
                        Text(f"{kind}:{label.plain}", style="cli.command.name"),
                        Text.from_markup(command.short_help or ""),
                    ),
                )


class _ScriptHelpContext(HelpContext):
    formatter_class = _ScriptHelpFormatter


class _RunnableCommand(CliCommand):
    """Show runnable help when its collected call is incomplete."""

    context_class = _ScriptHelpContext

    def __init__(self, *, flow: FlowDecl | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._flow = flow

    def make_parser(self, ctx: Context) -> _OptionParser:
        parser = _RunnableParser(ctx)
        for param in self.get_params(ctx):
            param.add_to_parser(parser, ctx)
        return parser

    def collect_usage_pieces(self, ctx: Context) -> list[str]:
        pieces = [self.options_metavar] if self.options_metavar else []
        names = {
            param.name
            for param in self.get_params(ctx)
            if isinstance(param, RunnableArgument)
        }
        if names - {"_"}:
            pieces.append("[ARGS]")
        if "_" in names:
            pieces.append("INPUT")
        return pieces

    def format_usage(self, ctx: Context, formatter: HelpFormatter) -> None:
        formatter.write_usage(
            ctx.command_path, " ".join(self.collect_usage_pieces(ctx))
        )

    def invoke(self, ctx: Context) -> Any:
        try:
            return TyperCommand.invoke(self, ctx)
        except _IncompleteRunnableInput:
            typer.echo(ctx.get_help(), color=ctx.color)
            ctx.exit(2)


class _ScriptGroup(CliGroup):
    """List runnable descriptions before the script's options."""

    context_class = _ScriptHelpContext

    def resolve_command(
        self, ctx: Context, args: list[str]
    ) -> tuple[str | None, Any, list[str]]:
        kind, separator, name = args[0].partition(":")
        if separator and kind in {"agic", "flow", "runnable"}:
            name = name.strip()
            if not name:
                raise ValueError(f"{kind} selector cannot be empty")
            command = self.get_command(ctx, name)
            if not isinstance(command, _RunnableCommand):
                raise ValueError(f"runnable not found: {name}")
            if kind == "agic" and command._flow is not None:
                raise ValueError(f"runnable is not an agic: {name}")
            if kind == "flow" and command._flow is None:
                raise ValueError(f"runnable is not a flow: {name}")
            args = [name, *args[1:]]
        return super().resolve_command(ctx, args)


def _flow_outline(flow: FlowDecl) -> Text:
    """Describe authored stages once, without expanding runnable calls."""

    outline = Text(no_wrap=True, overflow="ellipsis")

    def append_statements(statements: tuple[FlowStmt, ...], depth: int) -> None:
        for index, statement in enumerate(statements):
            if index:
                outline.append("\n")
            prefix = f"{'  ' * depth}[{index}] "
            doc = " ".join(statement.doc.split()) if statement.doc else ""
            if doc:
                outline.append(f"{prefix}{doc}\n")
                prefix = " " * len(prefix)
            outline.append(f"{prefix}{statement_description(statement)}\n")
            if isinstance(statement, RepeatStmt):
                append_statements(statement.stmts, depth + 1)

    append_statements(flow.stmts, 0)
    if not flow.stmts:
        outline.append("No statements.")
    outline.rstrip()
    return outline


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
        result = command.main(
            args=argv[1:] or ["--help"],
            prog_name=f"{prog_name} {argv[0]}",
            standalone_mode=False,
        )
        return int(result) if isinstance(result, int) else 0
    except typer.Exit as exc:
        return exc.exit_code
    except ClickException as exc:
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
    options = _runnable_command(
        None, program=program, source_path=source_path, stdin=stdin
    ).params
    group = _ScriptGroup(
        name=source_label,
        params=[param for param in options if isinstance(param, TyperOption)],
        help=f"Run runnables from {source_label}",
        no_args_is_help=True,
        rich_markup_mode="rich",
        subcommand_metavar="RUNNABLE",
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
    runnable: Runnable | None,
    *,
    program: Program,
    source_path: Path,
    stdin: TextIO,
) -> TyperCommand:
    def callback(
        ctx: typer.Context,
        allow: AllowOptions = None,
        limit: LimitOptions = None,
        model: Annotated[
            str | None,
            typer.Option(
                "--model",
                metavar="MODEL_SPEC",
                help="Set the model identity and parameters for this run",
            ),
        ] = None,
        sandbox: Annotated[
            str | None,
            typer.Option(
                "--sandbox",
                metavar="SANDBOX_SPEC",
                help="Execute this run in the selected sandbox",
            ),
        ] = None,
        save: Annotated[
            str | None,
            typer.Option(
                "--out",
                "-o",
                metavar="PATH",
                help="Save the Run result to PATH, or use - for stdout",
            ),
        ] = None,
        quiet: Annotated[
            bool,
            typer.Option(
                "--quiet", "-q", help="Suppress prepare and execution progress"
            ),
        ] = False,
        dev: Annotated[
            Path | None,
            typer.Option("--dev", metavar="PATH", help=DEVELOPMENT_WHEEL_HELP),
        ] = None,
        items: Annotated[list[str] | None, typer.Argument(hidden=True)] = None,
    ) -> int:
        assert runnable is not None
        inherited = ctx.parent.params if ctx.parent is not None else {}
        allow = [*inherited.get("allow", ()), *(allow or ())]
        limit = [*inherited.get("limit", ()), *(limit or ())]
        quiet = quiet or inherited.get("quiet", False)
        if ctx.get_parameter_source("model") != ParameterSource.COMMANDLINE:
            model = inherited.get("model", model)
        if ctx.get_parameter_source("sandbox") != ParameterSource.COMMANDLINE:
            sandbox = inherited.get("sandbox", sandbox)
        if ctx.get_parameter_source("save") != ParameterSource.COMMANDLINE:
            save = inherited.get("save", save)
        if ctx.get_parameter_source("dev") != ParameterSource.COMMANDLINE:
            # Group values have not passed through Typer's callback converters.
            root_dev = inherited.get("dev")
            dev = Path(root_dev) if root_dev is not None else dev
        override, input, raw_named = _collect_call(
            runnable, items=tuple(items or ()), stdin=stdin
        )
        override = _materialize_script_runnable_override(override, program=program)
        return _run(
            source_path,
            runnable=runnable.name,
            runnable_kind=runnable.kind,
            override=override,
            input=input,
            raw_named=raw_named,
            allow_options=tuple(allow),
            model_body=model,
            limit_options=tuple(limit),
            sandbox=sandbox,
            dev=dev,
            save=save,
            quiet=quiet,
        )

    name = runnable.name if runnable is not None else "script"
    kind = runnable.kind if runnable is not None else "runnable"
    doc = (runnable.doc or "").strip() if runnable is not None else ""
    command = get_command_from_info(
        CommandInfo(
            name=name,
            cls=_RunnableCommand,
            callback=callback,
            help=f"Run {kind} {name} - {doc}" if doc else f"Run {kind} {name}",
            short_help=doc or f"{kind.capitalize()} {name}",
        ),
        pretty_exceptions_short=True,
        rich_markup_mode="rich",
    )
    assert isinstance(command, _RunnableCommand)
    if runnable is not None:
        command._flow = runnable if isinstance(runnable, FlowDecl) else None
        arguments = runnable_parameters(
            runnable,
            input_help="- from stdin, -- starts input",
            help_only=True,
        )
        command.params[-1:-1] = arguments
    return command


def _public_runnables(program: Program) -> tuple[Runnable, ...]:
    return tuple(
        runnable
        for runnable in (*program.agics, *program.flows)
        if runnable.name != "default" and not runnable.name.startswith("<")
    )


def collect_named_arguments(
    runnable: Runnable,
    *,
    items: tuple[str, ...],
) -> tuple[CallInput[str], list[str]]:
    """Collect declared named arguments and leave primary input to the caller."""
    params = {parameter.name: parameter for parameter in runnable.params}
    raw_args: dict[str, str] = {}
    input_items: list[str] = []
    for index, item in enumerate(items):
        if item in {_LINE_INPUT_MARKER, "-"}:
            input_items = list(items[index:])
            break
        name, separator, value = item.partition("=")
        if separator and name in params:
            if name in raw_args:
                raise typer.BadParameter(f"argument {name} was provided more than once")
            raw_args[name] = value
            continue
        raise UsageError(f"unknown argument: {name}; use '--' to start input")

    return CallInput(raw_args), input_items


def _collect_call(
    runnable: Runnable,
    *,
    items: tuple[str, ...],
    stdin: TextIO,
) -> tuple[RunOverride, CallInput[str], CallInput[str]]:
    raw_args, input_items = collect_named_arguments(runnable, items=items)
    call_input = _input_source(input_items, stdin=stdin)
    call_source = call_input.get("_", "") if call_input is not None else ""
    override, input = parse_call(call_source)
    if call_input is not None and override.empty and set(input) <= {"_"}:
        input = parse_input(call_input)
    has_runnable_override = override.runnable is not None
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
            and "_" not in input
        ):
            raise _IncompleteRunnableInput
    return (
        override,
        input,
        CallInput(raw_args),
    )


def _materialize_script_runnable_override(
    override: RunOverride,
    *,
    program: Program,
) -> RunOverride:
    """Resolve input-local runnable selections against the authored program."""

    if override.runnable in {None, "default"}:
        return override
    dataset = runnable_dataset(program)
    matches = dataset.query(override.runnable)
    if len(matches) != 1:
        raise ValueError(f"runnable query is unknown or ambiguous: {override.runnable}")
    return replace(override, runnable=dataset.schema.exact_match_for(matches[0]))


def _input_source(items: list[str], *, stdin: TextIO) -> CallInput[str] | None:
    if items and items[0] == _LINE_INPUT_MARKER:
        value = _join_input_items(items[1:])
        if not value.strip():
            raise UsageError("line input requires nonempty text")
        return CallInput({"_": value})
    if items == ["-"]:
        return CallInput({"_": stdin.read()})
    if not stdin.isatty():
        value = stdin.read()
        return CallInput({"_": value}) if value else None
    return None


def _join_input_items(items: list[str]) -> str:
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


def _run(
    source_path: Path,
    *,
    runnable: str,
    runnable_kind: str,
    override: RunOverride,
    input: CallInput[str],
    raw_named: CallInput[str],
    allow_options: tuple[str, ...],
    model_body: str | None,
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
    runnable_ref = f"{runnable_kind}:{runnable}"
    try:
        layout = agents.materialize_roaming_program(source_path)
        session_override = _script_session_override(
            model_body=model_body,
            allow_options=allow_options,
            limit_options=limit_options,
        )
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
                        runnable=runnable_ref,
                        override=override,
                        input=input,
                        raw_named=raw_named,
                        session_override=session_override,
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
                        runnable=runnable_ref,
                        override=override,
                        input=input,
                        raw_named=raw_named,
                        session_override=session_override,
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
        message = (
            progress.failure_message(exc)
            if progress.failure_stage is not None
            else str(exc)
        )
        _error(message)
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


def _script_session_override(
    *,
    model_body: str | None,
    allow_options: tuple[str, ...],
    limit_options: tuple[str, ...],
) -> RunOverride:
    ceilings = resolve_ceiling_overrides({}, allow_options)
    limits = resolve_limit_overrides({}, limit_options)
    return RunOverride(
        model=parse_model_body(model_body) if model_body is not None else None,
        allow=tuple(
            AllowOverride(cast(AllowField, field), value)
            for field, value in ceilings.items()
        ),
        limits=tuple(
            LimitOverride(cast(LimitField, field), value)
            for field, value in limits.items()
        ),
    )


async def _execute_remote(
    *,
    layout: AgentLayout,
    endpoint: str,
    sandbox: str,
    runnable: str,
    override: RunOverride,
    input: CallInput[str],
    raw_named: CallInput[str],
    session_override: RunOverride,
    quiet: bool,
    on_accept: Callable[[str], None] | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> RunRecord:
    """Execute one script request through a validated AgentServer."""

    environ = load_runtime_environ(layout, base_environ=os.environ)
    request_input = _remote_script_input(input, raw_named=raw_named)
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
            surface_setting = await _remote_script_defaults(
                http,
                client.endpoint,
            )
            surface_setting = replace(surface_setting, runnable=runnable)
            session_setting = apply_session_setting(
                surface_setting,
                surface_setting,
                session_override,
            )
            ceilings, effective = materialize_run_setting(
                surface_setting,
                session_setting,
                _remote_script_override(override, runnable=runnable),
            )
            model = effective.model
            if model is not None and not is_concrete_model_ref(model.ref):
                models = await _remote_script_models(http, client.endpoint)
                model = replace(
                    model,
                    ref=materialize_model_selection(models, model.ref),
                )
            thread = await _create_remote_script_thread(http, client.endpoint)
            handle = await client.run(
                RunRequest(
                    thread_id=thread,
                    request_id=f"term_{uuid4().hex}",
                    runnable=RunnableRequest(
                        effective.runnable or runnable, request_input
                    ),
                    model=model,
                    policy=RunPolicy(allow=ceilings, limits=effective.limits),
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
    input: CallInput[str],
    *,
    raw_named: CallInput[str],
) -> CallInput[str]:
    """Encode CLI-surface named sources in the authored request input."""

    if any(name != "_" for name in input) and raw_named:
        raise ValueError("named inputs cannot be supplied by both source and surface")
    return CallInput({**raw_named, **input})


def _remote_script_override(
    override: RunOverride,
    *,
    runnable: str,
) -> RunOverride:
    """Keep `:runnable default` anchored to the dynamic CLI runnable."""

    return (
        replace(override, runnable=runnable)
        if override.runnable == "default"
        else override
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


async def _remote_script_defaults(
    client: httpx.AsyncClient,
    endpoint: str,
) -> SessionSetting:
    try:
        response = await client.get(f"{endpoint}/api/v1/runs/defaults")
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, Mapping) or set(payload) != {
            "model",
            "runnable",
            "policy",
        }:
            raise ValueError
        model = payload.get("model")
        runnable = payload.get("runnable")
        if not isinstance(runnable, str):
            raise ValueError
        model_request = (
            _MODEL_REQUEST_ADAPTER.validate_python(model) if model is not None else None
        )
        policy = _RUN_POLICY_ADAPTER.validate_python(payload.get("policy"))
        return SessionSetting(
            model=model_request,
            runnable=runnable,
            limits=policy.limits,
        )
    except (
        httpx.HTTPError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValidationError,
        ValueError,
    ) as exc:
        raise RuntimeError("remote script run defaults are invalid") from exc


async def _remote_script_models(
    client: httpx.AsyncClient,
    endpoint: str,
) -> Mapping[str, Any]:
    """Load one effective model list for request-ref materialization."""

    try:
        response = await client.get(f"{endpoint}/api/v1/models")
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, Mapping) or not isinstance(
            payload.get("items"), list
        ):
            raise ValueError
        return cast(Mapping[str, Any], payload)
    except (
        httpx.HTTPError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValueError,
    ) as exc:
        raise RuntimeError("remote script model list is invalid") from exc


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
    override: RunOverride,
    input: CallInput[str],
    raw_named: CallInput[str],
    session_override: RunOverride,
    quiet: bool,
) -> RunRecord:
    environ = load_runtime_environ(layout, base_environ=os.environ)
    allow_overrides = resolve_ceiling_overrides(environ)
    setup_watcher = SetupWatcher(
        layout,
        sandbox=sandbox,
        allow_overrides={
            name: value
            for name, value in allow_overrides.items()
            if name in {"models", "tools"}
        },
        default_overrides={
            **resolve_default_overrides(environ),
        },
        limit_overrides=resolve_limit_overrides(environ),
        compact_override=resolve_compact_override(environ),
    )
    state_watcher = StateWatcher(
        layout,
        allow_overrides={
            name: value
            for name, value in allow_overrides.items()
            if name in {"psyches", "skills", "services", "prompts"}
        },
        initial_state=state,
    )
    setup = await setup_watcher.refresh()
    state = await state_watcher.refresh()
    fallback_model = (
        setup.models.effective_default(None) if setup.defaults.model is None else None
    )
    executor = RunExecutor(
        store,
        ids,
        refresh_state=state_watcher.refresh_result,
    )
    spec = resolve_spec(
        override,
        input,
        setup=setup,
        state=state,
        thread=_UNPERSISTED_THREAD,
        default_runnable=runnable,
        surface=RunBindings(model=fallback_model, runnable=runnable),
        session_override=session_override,
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
        return await await_script_run(handle)
    finally:
        try:
            await executor.stop()
        finally:
            if tracer is not None:
                tracer.close()


async def await_script_run(handle: LocalRunHandle) -> RunRecord:
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
