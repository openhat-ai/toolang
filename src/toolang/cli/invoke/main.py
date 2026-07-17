"""Non-interactive agic and flow invocation."""

from __future__ import annotations

import os
from pathlib import Path
import sys

import click
import typer

from toolang.agent import local as agents
from toolang.agent import runtime as agent_up
from ...base.error import ToolangError
from ...config.env import load_runtime_environ
from ...execution.request import ExecutableKind
from ...lang.ast import AgicDecl, FlowDecl, Program
from ...state.agent import AgentState
from ..common.progress import CliProgress, as_progress_sink, make_cli_progress
from .help import show_help
from .rendering import ScriptProgressSink, emit_interrupt, emit_outcome, progress_sink
from .request import (
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
    for agic in program.available_agics:
        if executable_name(agic) == argv[0]:
            return "agic", agic, argv[1:]
    for flow in program.flows:
        if flow.name == argv[0]:
            return "flow", flow, argv[1:]
    raise click.ClickException(f"unknown target: {argv[0]}")
