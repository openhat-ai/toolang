"""Select one command before native Typer parameter parsing."""

from __future__ import annotations

from pathlib import Path

from typer._click import Command, Context
from typer._click.exceptions import ClickException, UsageError

from toolang.catalog.agent import LocalAgents
from ..common.agent_argument import agent_parameter, bind_prefix
from ..common.context import resolve_root
from ..common.help import CliGroup, show_help
from ..common.lazy import LazyCommand


def local_script_candidate(token: str) -> bool:
    """Recognize syntax, independently of existence and source validity."""
    return token.endswith(".too") and ":" not in token.split("/", 1)[0]


def _agent_candidate(ctx: Context, token: str) -> bool:
    if token.startswith("-"):
        return False
    if any(marker in token for marker in (":", "/", "\\", "@")) or token.endswith(
        ".too"
    ):
        return True
    explicit = ctx.params.get("toolang_root")
    root = resolve_root(Path(explicit) if explicit is not None else None)
    try:
        return LocalAgents(root / "agents").get(token) is not None
    except ValueError:
        return False


def resolve_command(
    group: CliGroup, ctx: Context, args: list[str]
) -> tuple[str | None, Command | None, list[str]]:
    """Keep known command precedence and inspect only the two header operands."""
    first = args[0]
    if first in group.commands:
        return CliGroup.resolve_command(group, ctx, args)
    if len(args) > 1 and args[1] in group.commands:
        name = args[1]
        command = group.commands[name]
        if isinstance(command, LazyCommand):
            command = command.load()
        agent = agent_parameter(command)
        if agent is None or "before" not in agent.contract.positions:
            message = (
                f"Command '{name}' does not accept AGENT."
                if agent is None
                else f"AGENT must follow '{name}'."
            )
            error = ClickException(message)
            error.exit_code = 2
            raise error
        bind_prefix(ctx, command, first)
        # Selected LazyCommand.make_context still applies the caller's UI hooks.
        return name, group.commands[name], args[2:]
    if local_script_candidate(first):
        from .commands.script import implicit_entry

        implicit_entry(ctx)
        return "run", group.commands["run"], args
    if _agent_candidate(ctx, first):
        if args[1:] in (["--help"], ["-h"]):
            show_help(ctx)
        if len(args) == 1:
            if ctx.resilient_parsing:
                return None, None, args
            raise UsageError("Missing command.", ctx)
        return CliGroup.resolve_command(group, ctx, args[1:])
    return CliGroup.resolve_command(group, ctx, args)
