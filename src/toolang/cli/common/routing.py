"""Typer routing and usage rendering shared by CLI entry points."""

from __future__ import annotations

from collections.abc import Mapping

from rich.text import Text
from typer._click import Context, HelpFormatter, Parameter
from typer._click.exceptions import MissingParameter
from typer.core import TyperArgument, TyperCommand, TyperGroup

from .context import CliContext
from .parameters import TextType
from .help import CliCommand, CliGroup, parameter_usage, show_help, write_usage


def _bound_agent(ctx: Context) -> bool:
    return isinstance(ctx.obj, CliContext) and ctx.obj.selector is not None


def _prefix_usage_path(ctx: Context, metavar: str) -> Text:
    if _bound_agent(ctx):
        return Text(ctx.command_path, style="cli.command.name")
    root, _, remainder = ctx.command_path.partition(" ")
    return Text.assemble(
        (root, "cli.command.name"),
        (f" {metavar}", "cli.usage"),
        (f" {remainder}" if remainder else "", "cli.command.name"),
    )


class PrefixAgentCommand(CliCommand):
    """Render one virtual prefix-agent argument in help output."""

    prefix_agent_metavar = "[AGENT]"
    argument_help = "Modify the agent's home caps; omit for root caps"

    def _real_params(self, ctx: Context) -> list[Parameter]:
        return TyperCommand.get_params(self, ctx)

    def _prefix_agent_argument(self) -> TyperArgument:
        return _HelpOnlyTyperArgument(
            param_decls=["agent"],
            metavar="AGENT",
            type=TextType(),
            required=False,
            default=None,
            expose_value=False,
            help=self.argument_help,
        )

    def get_params(self, ctx: Context) -> list[Parameter]:
        if _bound_agent(ctx):
            return self._real_params(ctx)
        return [self._prefix_agent_argument(), *self._real_params(ctx)]

    def parse_args(self, ctx: Context, args: list[str]) -> list[str]:
        if _bound_agent(ctx):
            return TyperCommand.parse_args(self, ctx, args)
        try:
            return TyperCommand.parse_args(self, ctx, args)
        except MissingParameter:
            show_help(ctx)

    def format_usage(self, ctx: Context, formatter: HelpFormatter) -> None:
        prefix_path = _prefix_usage_path(ctx, self.prefix_agent_metavar)
        write_usage(formatter, prefix_path, " ".join(self.collect_usage_pieces(ctx)))


class RequiredPrefixAgentGroup(CliGroup):
    """Render required AGENT between the CLI root and a command group."""

    prefix_agent_metavar = "<AGENT>"
    argument_help = "Local agent name"

    def get_params(self, ctx: Context) -> list[Parameter]:
        if _bound_agent(ctx):
            return super().get_params(ctx)
        agent = _HelpOnlyTyperArgument(
            param_decls=["agent"],
            metavar="AGENT",
            type=TextType(),
            required=True,
            expose_value=False,
            help=self.argument_help,
        )
        return [agent, *super().get_params(ctx)]

    def format_usage(self, ctx: Context, formatter: HelpFormatter) -> None:
        prefix_path = _prefix_usage_path(ctx, self.prefix_agent_metavar)
        pieces = [self.options_metavar] if self.options_metavar else []
        pieces.append(self.subcommand_metavar or "[SUBCOMMAND]")
        for param in self.get_params(ctx):
            pieces.extend(parameter_usage(param, ctx))
        write_usage(formatter, prefix_path, " ".join(pieces))


class PrefixAgentChoreGroup(RequiredPrefixAgentGroup):
    """Render required AGENT for the chore group."""


class OptionalPrefixAgentGroup(CliGroup):
    """Render optional AGENT between the runnable and command path."""

    prefix_agent_metavar = "[AGENT]"
    argument_help = "Local agent name; omit for root scope"

    def _real_params(self, ctx: Context) -> list[Parameter]:
        return TyperGroup.get_params(self, ctx)

    def _prefix_agent_argument(self) -> TyperArgument:
        return _HelpOnlyTyperArgument(
            param_decls=["agent"],
            metavar="AGENT",
            type=TextType(),
            required=False,
            default=None,
            expose_value=False,
            help=self.argument_help,
        )

    def get_params(self, ctx: Context) -> list[Parameter]:
        if _bound_agent(ctx):
            return self._real_params(ctx)
        return [self._prefix_agent_argument(), *self._real_params(ctx)]

    def format_usage(self, ctx: Context, formatter: HelpFormatter) -> None:
        prefix_path = _prefix_usage_path(ctx, self.prefix_agent_metavar)
        pieces = [self.options_metavar] if self.options_metavar else []
        pieces.append(self.subcommand_metavar or "[COMMAND] [ARGUMENTS]")
        write_usage(formatter, prefix_path, " ".join(pieces))


class OptionalPrefixAgentCommand(PrefixAgentCommand):
    prefix_agent_metavar = "[AGENT]"


class OptionalPrefixAgentListCommand(OptionalPrefixAgentCommand):
    argument_help = "Local agent name; omit for root caps only"


class OptionalPrefixAgentSetupCommand(OptionalPrefixAgentCommand):
    argument_help = "Local agent name; omit for root configuration"


class RequiredPrefixAgentCommand(PrefixAgentCommand):
    prefix_agent_metavar = "<AGENT>"
    argument_help = "Local agent name"

    def _prefix_agent_argument(self) -> TyperArgument:
        return _HelpOnlyTyperArgument(
            param_decls=["agent"],
            metavar="AGENT",
            type=TextType(),
            required=True,
            default=None,
            expose_value=False,
            help=self.argument_help,
        )

    def parse_args(self, ctx: Context, args: list[str]) -> list[str]:
        state = ctx.obj
        if not isinstance(state, CliContext):
            raise TypeError("missing CLI context")
        remaining = PrefixAgentCommand.parse_args(self, ctx, args)
        if not state.agent:
            show_help(ctx)
        return remaining


class _HelpOnlyTyperArgument(TyperArgument):
    """One help-only argument that never participates in parsing."""

    def get_usage_pieces(self, ctx: Context) -> list[str]:
        # Prefix placement is owned by the command, not the context path.
        return []

    def add_to_parser(self, parser: object, ctx: Context) -> None:
        del parser, ctx

    def handle_parse_result(
        self,
        ctx: Context,
        opts: Mapping[str, object],
        args: list[str],
    ) -> tuple[None, list[str]]:
        del ctx, opts
        return None, args
