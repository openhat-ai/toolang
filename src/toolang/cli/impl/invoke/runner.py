"""Non-interactive agic and flow invocation."""

from __future__ import annotations

import os
from pathlib import Path
import sys

import click
import typer

from toolang.up import process as agents
from toolang.up import server as agent_up
from toolang.base.types.sandbox import SandboxSelector
from toolang.common.errors import ToolangError
from toolang.up.logging import resolve_agent_logging
from toolang.cli.common.client import (
    RuntimeClient,
    owned_runtime_client,
)
from toolang.cli.common.errors import RuntimeClientError
from toolang.cli.common.context import load_runtime_environ
from toolang.execution.executor.prepare import effective_agics
from toolang.execution.records import RunRecord
from toolang.lang.ast import AgicDecl, FlowDecl, Program
from toolang.state.state import AgentState
from toolang.cli.common.progress import CliProgress, as_progress_sink, make_cli_progress
from .help import show_help
from .rendering import ScriptProgressSink, emit_interrupt, emit_outcome, progress_sink
from .request import (
    ExecutableKind,
    MissingInvokeInput,
    RoamingInvokeRequest,
    consume_control_options,
    executable_name,
    parse_request,
)

HELP_FLAGS = {"--help", "-h"}


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


def handle_roaming_invoke(
    global_args: list[str], body: list[str], *, prog_name: str
) -> int:
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
    (
        quiet,
        verbosity,
        leading_models,
        leading_tools,
        leading_caps,
        leading_sandbox,
        normalized_remaining,
    ) = consume_control_options(remaining)
    prepare_progress = _prepare_progress(quiet=quiet, argv=remaining)
    script_progress: ScriptProgressSink | None = None
    request: RoamingInvokeRequest | None = None
    runtime_environ: dict[str, str] | None = None
    toolang_root: Path | None = None
    agent_name: str | None = None
    try:
        toolang_root, agent_name, state, program = _load_roaming_program(
            source_path,
            progress=as_progress_sink(prepare_progress),
        )
        if prepare_progress is not None:
            prepare_progress.finish(details=False)
        if normalized_remaining and normalized_remaining[0] in HELP_FLAGS:
            show_help(source_label, program, target_name=None, prog_name=prog_name)
            return 0
        if not remaining:
            show_help(source_label, program, target_name=None, prog_name=prog_name)
            return 0
        if not normalized_remaining:
            show_help(source_label, program, target_name=None, prog_name=prog_name)
            return 0
        executable_kind, executable, remainder = _select_roaming_executable(
            program, normalized_remaining
        )
        if any(token in HELP_FLAGS for token in remainder):
            show_help(
                source_label,
                program,
                target_name=executable_name(executable),
                prog_name=prog_name,
            )
            return 0
        try:
            request = parse_request(
                executable,
                remainder,
                executable_kind=executable_kind,
                leading_verbosity=verbosity,
                leading_models=leading_models,
                leading_tools=leading_tools,
                leading_caps=leading_caps,
                leading_sandbox=leading_sandbox,
            )
        except MissingInvokeInput:
            show_help(
                source_label,
                program,
                target_name=executable_name(executable),
                prog_name=prog_name,
            )
            return 0
        runtime_environ = load_runtime_environ(
            toolang_root, agent_name, base_environ=os.environ
        )
        script_progress = progress_sink(
            executable_name=request.executable_name,
            quiet=quiet or request.quiet,
            verbosity=request.verbosity,
        )
        metadata: dict[str, object] = {
            "invoke_params": request.invoke_params,
            "invoke_parts": request.invoke_parts,
        }
        selector = SandboxSelector.parse(request.sandbox or "none")
        if selector.driver == "none":
            outcome = agent_up.invoke(
                toolang_root=toolang_root,
                agent_name=agent_name,
                executable_kind=request.executable_kind,
                executable_name=request.executable_name,
                input_text=request.input_text,
                models=request.models,
                tools=request.tools or None,
                caps=request.caps or None,
                metadata=metadata,
                environ=runtime_environ,
                reply=script_progress,
                agent_state=state,
            )
        else:
            outcome = _invoke_hosted(
                toolang_root=toolang_root,
                agent_name=agent_name,
                request=request,
                metadata=metadata,
                environ=runtime_environ,
                state=state,
                reply=script_progress,
            )
    except KeyboardInterrupt:
        if script_progress is not None:
            script_progress.interrupt()
        if prepare_progress is not None:
            prepare_progress.interrupt()
        emit_interrupt(
            script_progress=script_progress,
            toolang_root=toolang_root,
            agent_name=agent_name,
            executable_name=request.executable_name if request is not None else None,
            environ=runtime_environ,
        )
        return 130
    except (
        FileExistsError,
        FileNotFoundError,
        ValueError,
        ToolangError,
        click.ClickException,
        RuntimeClientError,
    ) as exc:
        if prepare_progress is not None:
            prepare_progress.finish(details=False)
        message = exc.message if isinstance(exc, click.ClickException) else str(exc)
        typer.echo(f"toolang error: {message}", err=True)
        return 1
    return emit_outcome(
        outcome,
        toolang_root=toolang_root,
        agent_name=agent_name,
        executable_name=request.executable_name,
    )


def _invoke_hosted(
    *,
    toolang_root: Path,
    agent_name: str,
    request: RoamingInvokeRequest,
    metadata: dict[str, object],
    environ: dict[str, str],
    state: AgentState,
    reply: ScriptProgressSink,
) -> RunRecord:
    selector = SandboxSelector.parse(request.sandbox or "none")
    process = agents.AgentProcess(toolang_root, agent_name)
    status = process.status(ui_base_url="")
    if status is not None and status.status in {"running", "preparing", "starting"}:
        if status.status != "running" or status.endpoint is None:
            raise click.ClickException(f"agent API is not ready: {agent_name}")
        if not _sandbox_matches(status.sandbox, selector):
            raise click.ClickException(
                f"agent is already running in sandbox {status.sandbox or 'unknown'}; "
                f"cannot use {selector.render()} for this run"
            )
        return _invoke_client(RuntimeClient(status.endpoint), request, metadata, reply)

    runtime_environ = dict(environ)
    runtime_environ["TOOLANG_ROOT"] = str(toolang_root)
    log_plan = resolve_agent_logging(
        mode="start",
        environ=runtime_environ,
        agent_log_path=agents.agent_runtime_log_path(toolang_root, agent_name),
    )
    startup = agent_up.resolve_startup(
        toolang_root=toolang_root,
        agent_name=agent_name,
        sandbox=selector.render(),
        models=request.models,
        tools=request.tools or None,
        caps=request.caps,
        log_spec=log_plan.spec,
        environ=log_plan.environ,
        agent_state=state,
    )
    if log_plan.path is None:
        raise click.ClickException("agent log path was not resolved")
    with owned_runtime_client(
        root=toolang_root,
        name=agent_name,
        startup=startup,
        environ=log_plan.environ,
        log_path=log_plan.path,
    ) as client:
        return _invoke_client(client, request, metadata, reply)


def _invoke_client(
    client: RuntimeClient,
    request: RoamingInvokeRequest,
    metadata: dict[str, object],
    reply: ScriptProgressSink,
) -> RunRecord:
    return client.invoke(
        {
            "executable_kind": request.executable_kind,
            "executable_name": request.executable_name,
            "input": request.input_text or "",
            "models": list(request.models),
            "tools": list(request.tools) if request.tools else None,
            "caps": list(request.caps),
            "metadata": metadata,
        },
        on_event=reply.on_event,
    )


def _sandbox_matches(actual: str | None, requested: SandboxSelector) -> bool:
    if actual == requested.render():
        return True
    return (
        requested.target is None
        and isinstance(actual, str)
        and (actual.partition(":")[0] == requested.driver)
    )


def _unsupported_roaming_global_args(global_args: list[str]) -> bool:
    return bool(global_args)


def _prepare_progress(*, quiet: bool, argv: list[str]) -> "CliProgress | None":
    if quiet or not sys.stderr.isatty() or any(token in HELP_FLAGS for token in argv):
        return None
    return make_cli_progress()


def _load_roaming_program(
    source_path: Path,
    *,
    progress=None,
) -> tuple[Path, str, AgentState, Program]:
    toolang_root, agent_name = agents.materialize_roaming_program(source_path)
    state = agent_up.prepare_agent(
        toolang_root=toolang_root, agent_name=agent_name, progress=progress
    )
    return toolang_root, agent_name, state, state.program


def _select_roaming_executable(
    program: Program,
    argv: list[str],
) -> tuple[ExecutableKind, AgicDecl | FlowDecl, list[str]]:
    for agic in effective_agics(program):
        if executable_name(agic) == argv[0]:
            return "agic", agic, argv[1:]
    for flow in program.flows:
        if flow.name == argv[0]:
            return "flow", flow, argv[1:]
    raise click.ClickException(f"unknown target: {argv[0]}")
