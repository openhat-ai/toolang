"""Argument routing for the Toolang CLI entry point."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import typer

from toolang.agent import local as agents
from ...config.log import configure_logging
from ..caps.commands import CAP_KINDS
from ..common.routing import extract_root_args
from .commands import runtime

TOP_LEVEL_COMMANDS = frozenset(
    {
        "new",
        "clone",
        "remove",
        "list",
        "info",
        "hidden",
        "fmt",
        "parse",
        "model",
        "tool",
        "channel",
        "sandbox",
        "chat",
        "send",
        "attach",
        "threads",
        "runs",
        "inspect",
        "steer",
        "cancel",
        "rewind",
        "fork",
        "run",
        "start",
        "stop",
        "caps",
        *CAP_KINDS,
        "task",
        "chore",
    }
)
POSTFIX_AGENT_COMMANDS = frozenset(
    {
        "run",
        "start",
        "stop",
        "info",
        "chat",
        "send",
        "attach",
        "threads",
        "runs",
        "inspect",
        "steer",
        "cancel",
        "rewind",
        "fork",
    }
)
PREFIX_AGENT_COMMANDS = frozenset(
    {
        "run",
        "start",
        "stop",
        "caps",
        "chat",
        "send",
        "attach",
        "threads",
        "runs",
        "inspect",
        "steer",
        "cancel",
        "rewind",
        "fork",
        *CAP_KINDS,
        "task",
        "chore",
    }
)
THREAD_TARGET_COMMANDS = frozenset({"steer", "cancel", "rewind", "fork"})
ROAMING_THREAD_COMMANDS = frozenset(
    {"threads", "runs", "inspect", "steer", "cancel", "rewind", "fork"}
)


def dispatch_roaming(
    argv: list[str],
    *,
    prog_name: str,
    run_app: Callable[[list[str], str | None], int],
) -> int | None:
    global_args, body = extract_root_args(argv)
    if not body or (source := _source_path(body[0])) is None:
        return None
    try:
        configure_logging(spec=None, environ={})
    except ValueError as exc:
        typer.echo(f"toolang error: {exc}", err=True)
        return 1
    if len(body) >= 2 and body[1] in ROAMING_THREAD_COMMANDS:
        if global_args:
            return _unsupported_global_options()
        try:
            root, agent = agents.materialize_roaming_program(source)
        except (FileExistsError, FileNotFoundError, ValueError) as exc:
            typer.echo(f"toolang error: {exc}", err=True)
            return 1
        return run_app(["--root", str(root), *body[1:]], agent)
    if runtime.is_roaming_file_request(body[1:]):
        if global_args:
            return _unsupported_global_options()
        return runtime.run_roaming_file(source, body[1:])

    from ..invoke import main as invoke

    return invoke.handle_roaming_invoke(global_args, body, prog_name=prog_name)


def normalize(argv: list[str]) -> tuple[list[str], str | None]:
    global_args, body = extract_root_args(argv)
    rewritten_body, agent = _rewrite_agent_shortcuts(body)
    return [*global_args, *rewritten_body], agent


def _rewrite_agent_shortcuts(body: list[str]) -> tuple[list[str], str | None]:
    if not body:
        return body, None
    agent = body[0]
    if not _looks_like_agent_name(agent) or len(body) < 2:
        return body, None
    command = body[1]
    if len(body) == 2 and command in THREAD_TARGET_COMMANDS:
        return [command, "--help"], agent
    if command in POSTFIX_AGENT_COMMANDS:
        return [command, agent, *body[2:]], None
    if command in PREFIX_AGENT_COMMANDS:
        return [command, *body[2:]], agent
    return body, None


def _source_path(token: str) -> Path | None:
    text = token.strip()
    if not text or text.startswith("-"):
        return None
    try:
        source = Path(text).expanduser().resolve()
    except OSError:
        return None
    return source if source.is_file() and source.suffix == ".too" else None


def _looks_like_agent_name(token: str) -> bool:
    return bool(token) and not token.startswith("-") and token not in TOP_LEVEL_COMMANDS


def _unsupported_global_options() -> int:
    typer.echo(
        "toolang error: too <path>.too does not support global CLI options",
        err=True,
    )
    return 1
