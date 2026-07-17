"""Typer routing and usage rendering shared by CLI entry points."""

from __future__ import annotations

from collections.abc import Sequence

import click
from typer import rich_utils
from typer.core import TyperArgument, TyperCommand, TyperGroup

from .context import CliContext

# Typer renders command help text dim by default. Normal weight keeps usage
# notes readable across terminal themes.
setattr(rich_utils, "STYLE_HELPTEXT", "")


class PrefixAgentCommand(TyperCommand):
    """Render one virtual prefix-agent argument in help output."""

    prefix_agent_metavar = "[AGENT]"
    argument_metavar = "TEXT"
    argument_help = "Apply to this agent's home caps instead of root caps."

    def _real_params(self, ctx: click.Context) -> list[click.Parameter]:
        return TyperCommand.get_params(self, ctx)

    def _prefix_agent_argument(self) -> click.Argument:
        return _HelpOnlyTyperArgument(
            param_decls=["agent"],
            metavar=self.argument_metavar,
            required=False,
            default=None,
            expose_value=False,
            help=self.argument_help,
        )

    def get_params(self, ctx: click.Context) -> list[click.Parameter]:
        return [self._prefix_agent_argument(), *self._real_params(ctx)]

    def format_usage(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        command_path = _strip_help_only_agent_metavars(ctx.command_path)
        root_name, _, remainder = command_path.partition(" ")
        prefix_path = (
            f"{root_name} {self.prefix_agent_metavar} {remainder}"
            if remainder
            else f"{root_name} {self.prefix_agent_metavar}"
        )
        pieces = [self.options_metavar] if self.options_metavar else []
        for param in self._real_params(ctx):
            pieces.extend(param.get_usage_pieces(ctx))
        formatter.write_usage(prefix_path, " ".join(pieces))


class PrefixAgentJobGroup(TyperGroup):
    """Render required AGENT between the CLI root and group name."""

    prefix_agent_metavar = "AGENT"

    def format_usage(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        command_path = _strip_help_only_agent_metavars(ctx.command_path)
        root_name, _, remainder = command_path.partition(" ")
        prefix_path = (
            f"{root_name} {self.prefix_agent_metavar} {remainder}"
            if remainder
            else f"{root_name} {self.prefix_agent_metavar}"
        )
        pieces = [self.options_metavar] if self.options_metavar else []
        pieces.append(self.subcommand_metavar or "[SUBCOMMAND]")
        for param in self.get_params(ctx):
            pieces.extend(param.get_usage_pieces(ctx))
        formatter.write_usage(prefix_path, " ".join(pieces))


class OptionalPrefixAgentGroup(TyperGroup):
    """Render optional AGENT between the executable and command path."""

    prefix_agent_metavar = "[AGENT]"
    argument_metavar = "TEXT"
    argument_help = "Apply to this agent's home caps instead of root caps."

    def _real_params(self, ctx: click.Context) -> list[click.Parameter]:
        return TyperGroup.get_params(self, ctx)

    def _prefix_agent_argument(self) -> click.Argument:
        return _HelpOnlyTyperArgument(
            param_decls=["agent"],
            metavar=self.argument_metavar,
            required=False,
            default=None,
            expose_value=False,
            help=self.argument_help,
        )

    def get_params(self, ctx: click.Context) -> list[click.Parameter]:
        return [self._prefix_agent_argument(), *self._real_params(ctx)]

    def format_usage(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        command_path = _strip_help_only_agent_metavars(ctx.command_path)
        root_name, _, remainder = command_path.partition(" ")
        prefix_path = (
            f"{root_name} {self.prefix_agent_metavar} {remainder}"
            if remainder
            else f"{root_name} {self.prefix_agent_metavar}"
        )
        pieces = [self.options_metavar] if self.options_metavar else []
        pieces.append(self.subcommand_metavar or "[COMMAND] [ARGS]...")
        formatter.write_usage(prefix_path, " ".join(pieces))


class OptionalPrefixAgentCommand(PrefixAgentCommand):
    prefix_agent_metavar = "[AGENT]"


class OptionalPrefixAgentListCommand(OptionalPrefixAgentCommand):
    argument_help = "Also include this agent's home caps."


class RequiredPrefixAgentCommand(PrefixAgentCommand):
    prefix_agent_metavar = "AGENT"
    argument_help = "Agent name."

    def _prefix_agent_argument(self) -> click.Argument:
        return _HelpOnlyTyperArgument(
            param_decls=["agent"],
            metavar=self.argument_metavar,
            required=True,
            default=None,
            expose_value=False,
            help=self.argument_help,
        )

    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        state = ctx.obj
        if not isinstance(state, CliContext):
            raise TypeError("missing CLI context")
        if args and not args[0].startswith("-") and not state.agent:
            state.agent = args.pop(0)
        if not state.agent and "--help" not in args:
            click.echo(ctx.get_help())
            ctx.exit()
        return TyperCommand.parse_args(self, ctx, args)


class RuntimeAgentCommand(TyperCommand):
    """Render one required agent argument before the command name in help."""

    usage_agent_metavar = "AGENT"
    argument_help = "Agent name."

    def _real_params(self, ctx: click.Context) -> list[click.Parameter]:
        return TyperCommand.get_params(self, ctx)

    def _visible_real_params(self, ctx: click.Context) -> list[click.Parameter]:
        return [
            param
            for param in self._real_params(ctx)
            if not getattr(param, "hidden", False)
        ]

    def _help_agent_argument(self) -> click.Argument:
        return _HelpOnlyTyperArgument(
            param_decls=["agent"],
            metavar="TEXT",
            required=True,
            default=None,
            expose_value=False,
            help=self.argument_help,
        )

    def get_params(self, ctx: click.Context) -> list[click.Parameter]:
        return [self._help_agent_argument(), *self._real_params(ctx)]

    def format_usage(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        root_name, _, remainder = ctx.command_path.partition(" ")
        prefix_path = (
            f"{root_name} {self.usage_agent_metavar} {remainder}"
            if remainder
            else f"{root_name} {self.usage_agent_metavar}"
        )
        pieces = [self.options_metavar] if self.options_metavar else []
        for param in self._visible_real_params(ctx):
            pieces.extend(param.get_usage_pieces(ctx))
        formatter.write_usage(prefix_path, " ".join(pieces))


class RunAgentCommand(RuntimeAgentCommand):
    argument_help = "Existing local agent name, remote agent ref, or URL."

    def format_usage(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        pieces = [self.options_metavar] if self.options_metavar else []
        for param in self._visible_real_params(ctx):
            pieces.extend(param.get_usage_pieces(ctx))
        pieces.append(self.usage_agent_metavar)
        formatter.write_usage(ctx.command_path, " ".join(pieces))


class StartAgentCommand(RuntimeAgentCommand):
    argument_help = "Existing local agent name."


class OptionalPrefixAgentTemplateCommand(OptionalPrefixAgentCommand):
    def _help_template_argument(self) -> click.Argument:
        return _HelpOnlyTyperArgument(
            param_decls=["name"],
            metavar="TEXT",
            required=False,
            default=None,
            expose_value=False,
            help="Template name.",
        )

    def get_params(self, ctx: click.Context) -> list[click.Parameter]:
        return [
            self._prefix_agent_argument(),
            self._help_template_argument(),
            *self._real_params(ctx),
        ]


def normalize_components(components: Sequence[str] | None) -> list[str] | None:
    if components is None:
        return None
    normalized: list[str] = []
    for item in components:
        normalized.extend(value.strip() for value in item.split(",") if value.strip())
    return normalized


class _HelpOnlyTyperArgument(TyperArgument):
    """One help-only argument that never participates in parsing."""

    def make_metavar(self, ctx: click.Context | None = None) -> str:
        del ctx
        return self.metavar or "TEXT"

    def add_to_parser(self, parser: object, ctx: click.Context) -> None:
        del parser, ctx

    def handle_parse_result(
        self,
        ctx: click.Context,
        opts: click.core.cabc.Mapping[str, object],
        args: list[str],
    ) -> tuple[None, list[str]]:
        del ctx, opts
        return None, args


def _strip_help_only_agent_metavars(command_path: str) -> str:
    return " ".join(part for part in command_path.split() if part != "TEXT")
