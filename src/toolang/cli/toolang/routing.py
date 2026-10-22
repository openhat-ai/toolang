"""Select one command before native Typer parameter parsing."""

from __future__ import annotations

from typer._click import Command, Context
from typer._click.exceptions import UsageError

from ..common.agent_argument import agent_parameters, bind_prefix
from ..common.help import CliGroup, show_help
from ..common.lazy import LazyCommand


def local_script_candidate(token: str) -> bool:
    """Recognize syntax, independently of existence and source validity."""
    return token.endswith(".too") and ":" not in token.split("/", 1)[0]


def _agent_candidate(token: str) -> bool:
    """Recognize explicit selector syntax without consulting installed homes."""
    return not token.startswith("-") and (
        any(marker in token for marker in (":", "/", "\\", "@"))
        or token.endswith(".too")
    )


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
        if not agent_parameters(command):
            if ctx.resilient_parsing:
                return None, None, args
            with command.context_class(command, info_name=name, parent=ctx) as selected:
                raise UsageError(
                    f"Command {name!r} does not accept an agent prefix.", selected
                )
        bind_prefix(ctx, command, first)
        tail = args[2:]
        # Selected LazyCommand.make_context still applies the caller's UI hooks.
        return name, group.commands[name], tail
    if local_script_candidate(first):
        from .commands.script import implicit_entry

        implicit_entry(ctx)
        # The root has recognized FILE; preserve it as a literal in run's header.
        return "run", group.commands["run"], ["--", *args]
    if ctx.resilient_parsing and len(args) == 1 and not first.startswith("-"):
        bind_prefix(ctx, group, first)
        return None, None, args
    if _agent_candidate(first):
        if not ctx.resilient_parsing and args[1:] in (["--help"], ["-h"]):
            show_help(ctx)
        if len(args) == 1:
            if ctx.resilient_parsing:
                return None, None, args
            raise UsageError("Missing command.", ctx)
        return CliGroup.resolve_command(group, ctx, args[1:])
    return CliGroup.resolve_command(group, ctx, args)
