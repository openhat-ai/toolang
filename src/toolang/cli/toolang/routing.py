"""Dynamic agent groups within the native Typer command tree."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from typer._click import Command, Context
from typer._click.core import ParameterSource
from typer._click.exceptions import BadParameter, ClickException, UsageError

from toolang.common.layout import AgentPlacement, is_agent_name
from toolang.common.typer.ui import inherit_ui
from ..common.context import CliContext, parse_target, target_context
from ..common.help import CliGroup
from ..common.lazy import LazyCommand

if TYPE_CHECKING:
    from toolang.up.source import AgentTarget

ALL_AGENTS = frozenset[AgentPlacement]({"resident", "visiting", "roaming"})
RESIDENT = frozenset[AgentPlacement]({"resident"})


def agent_group(
    ctx: Context, name: str, commands: Mapping[str, Command]
) -> Command | None:
    """Recognize the root operand and return its native agent group."""
    if not (is_agent_name(name) or name.endswith(".too") or ":" in name or "/" in name):
        return None
    try:
        target = parse_target(name)
    except ValueError as exc:
        # Typer adds command suggestions to every UsageError raised during lookup.
        # A malformed target has already been recognized and is not a command typo.
        error = ClickException(
            BadParameter(str(exc), param_hint="AGENT", ctx=ctx).format_message()
        )
        error.exit_code = 2
        raise error from exc
    return inherit_ui(AgentGroup(name, target, commands), ctx)


class AgentGroup(CliGroup):
    """Expose supported commands for one selected agent using Typer lookup."""

    def __init__(
        self, name: str, target: AgentTarget | Path, commands: Mapping[str, Command]
    ) -> None:
        self.target = target
        self.root_commands = commands
        self.placement: AgentPlacement = (
            "roaming"
            if isinstance(target, Path)
            else "resident"
            if target.form == "name"
            else "visiting"
        )
        available = {}
        for command_name, command in commands.items():
            if not isinstance(command, LazyCommand):
                continue
            if self.placement not in command.agent_placements:
                continue
            available[command_name] = command
        label = (
            target.name
            if not isinstance(target, Path) and target.form == "name"
            else name
        )
        super().__init__(
            name=name,
            commands=available,
            help=f"Run and manage agent {label}",
            no_args_is_help=True,
        )

    def make_context(
        self,
        info_name: str | None,
        args: list[str],
        parent: Context | None = None,
        **extra: Any,
    ) -> Context:
        if parent is None:
            raise TypeError("agent group requires a root context")
        if isinstance(self.target, Path) and (
            not args or args[0] not in self.root_commands
        ):
            from .commands.script import source_command

            if (
                parent.get_parameter_source("toolang_root")
                == ParameterSource.COMMANDLINE
            ):
                raise UsageError(
                    "Script invocation does not support global --root / -r", parent
                )
            command = inherit_ui(
                source_command(self.target, self.name or str(self.target)), parent
            )
            return command.make_context(info_name, args, parent=parent, **extra)
        try:
            state = parent.obj
            if not isinstance(state, CliContext):
                raise TypeError("missing CLI context")
            extra["obj"] = target_context(state.root, self.target)
        except ValueError as exc:
            raise BadParameter(str(exc), param_hint="AGENT", ctx=parent) from exc
        return super().make_context(info_name, args, parent=parent, **extra)

    def get_command(self, ctx: Context, cmd_name: str) -> Command | None:
        command = super().get_command(ctx, cmd_name)
        if command is not None or ctx.resilient_parsing:
            return command
        known = self.root_commands.get(cmd_name)
        if known is not None:
            if not isinstance(known, LazyCommand) or not known.agent_placements:
                message = f"Command {cmd_name!r} is only available at the top level."
            else:
                placement = {
                    "resident": "resident",
                    "visiting": "remote",
                    "roaming": "local .too",
                }[self.placement]
                message = (
                    f"Command {cmd_name!r} is not available for {placement} agents."
                )
            # A known command is not a typo; keep Typer's suggestions for unknown names.
            error = ClickException(message)
            error.exit_code = 2
            raise error
        return None
