"""Typer routing and usage rendering shared by CLI entry points."""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from pathlib import Path

from rich.text import Text
from typer._click import Context, HelpFormatter, Parameter
from typer._click.exceptions import MissingParameter
from typer.core import TyperArgument, TyperCommand, TyperGroup

from .context import CliContext
from .parameters import TextType
from .help import CliCommand, CliGroup, parameter_usage, show_help, write_usage


def extract_root_args(
    argv: Sequence[str],
    *,
    extra_value_options: Collection[str] = (),
) -> tuple[list[str], list[str]]:
    """Separate root options from command arguments without crossing `--`."""

    value_options = {"--root", "-r", *extra_value_options}
    long_value_options = {option for option in value_options if option.startswith("--")}
    root_args: list[str] = []
    body: list[str] = []
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--":
            body.extend(argv[index:])
            break
        if token in value_options:
            step = 2 if index + 1 < len(argv) else 1
            root_args.extend(argv[index : index + step])
            index += step
            continue
        for option in long_value_options:
            prefix = f"{option}="
            if token.startswith(prefix):
                root_args.extend((option, token.removeprefix(prefix)))
                index += 1
                break
        else:
            body.append(token)
            index += 1
            continue
        continue
    return root_args, body


def explicit_root(args: Sequence[str]) -> Path | None:
    """Return the last explicit root value from extracted global arguments."""

    root: Path | None = None
    index = 0
    while index < len(args):
        if args[index] in {"--root", "-r"} and index + 1 < len(args):
            root = Path(args[index + 1])
            index += 2
            continue
        index += 1
    return root


def explicit_agent(token: str) -> str | None:
    """Parse one explicit resident target used to escape command names."""

    prefix, separator, name = token.partition(":")
    if prefix != "agent" or not separator:
        return None
    name = name.strip()
    if not name or name in {".", ".."} or "/" in name or "\\" in name:
        raise ValueError(f"invalid resident agent target: {token}")
    return name


def _prefix_usage_path(ctx: Context, metavar: str) -> Text:
    root, _, remainder = ctx.command_path.partition(" ")
    return Text.assemble(
        (root, "cli.command.name"),
        (f" {metavar}", "cli.usage"),
        (f" {remainder}" if remainder else "", "cli.command.name"),
    )


class PrefixAgentCommand(CliCommand):
    """Render one virtual prefix-agent argument in help output."""

    prefix_agent_metavar = "[AGENT]"
    argument_help = "Apply to this agent's home caps instead of root caps"

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
        return [self._prefix_agent_argument(), *self._real_params(ctx)]

    def parse_args(self, ctx: Context, args: list[str]) -> list[str]:
        try:
            return TyperCommand.parse_args(self, ctx, args)
        except MissingParameter:
            show_help(ctx)

    def format_usage(self, ctx: Context, formatter: HelpFormatter) -> None:
        prefix_path = _prefix_usage_path(ctx, self.prefix_agent_metavar)
        pieces = [self.options_metavar] if self.options_metavar else []
        for param in self._real_params(ctx):
            pieces.extend(parameter_usage(param, ctx))
        write_usage(formatter, prefix_path, " ".join(pieces))


class RequiredPrefixAgentGroup(CliGroup):
    """Render required AGENT between the CLI root and a command group."""

    prefix_agent_metavar = "AGENT"

    def get_params(self, ctx: Context) -> list[Parameter]:
        agent = _HelpOnlyTyperArgument(
            param_decls=["agent"],
            metavar="AGENT",
            type=TextType(),
            required=True,
            expose_value=False,
            help="Agent name",
        )
        return [agent, *super().get_params(ctx)]

    def format_usage(self, ctx: Context, formatter: HelpFormatter) -> None:
        prefix_path = _prefix_usage_path(ctx, self.prefix_agent_metavar)
        pieces = [self.options_metavar] if self.options_metavar else []
        pieces.append(self.subcommand_metavar or "[SUBCOMMAND]")
        for param in self.get_params(ctx):
            pieces.extend(parameter_usage(param, ctx))
        write_usage(formatter, prefix_path, " ".join(pieces))


class PrefixAgentJobGroup(RequiredPrefixAgentGroup):
    """Render required AGENT for the existing task and chore groups."""


class OptionalPrefixAgentGroup(CliGroup):
    """Render optional AGENT between the runnable and command path."""

    prefix_agent_metavar = "[AGENT]"
    argument_help = "Apply to this agent's home caps instead of root caps"

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
        return [self._prefix_agent_argument(), *self._real_params(ctx)]

    def format_usage(self, ctx: Context, formatter: HelpFormatter) -> None:
        prefix_path = _prefix_usage_path(ctx, self.prefix_agent_metavar)
        pieces = [self.options_metavar] if self.options_metavar else []
        pieces.append(self.subcommand_metavar or "[COMMAND] [ARGS]")
        write_usage(formatter, prefix_path, " ".join(pieces))


class OptionalPrefixAgentCommand(PrefixAgentCommand):
    prefix_agent_metavar = "[AGENT]"


class OptionalPrefixAgentListCommand(OptionalPrefixAgentCommand):
    argument_help = "Also include this agent's home caps"


class OptionalPrefixAgentModelsCommand(OptionalPrefixAgentCommand):
    argument_help = "Use this agent's model catalog and configuration"


class RequiredPrefixAgentCommand(PrefixAgentCommand):
    prefix_agent_metavar = "AGENT"
    argument_help = "Agent name"

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


class RuntimeAgentCommand(CliCommand):
    """Render one required agent argument before the command name in help."""

    usage_agent_metavar = "AGENT"
    argument_help = "Agent name"

    def _real_params(self, ctx: Context) -> list[Parameter]:
        return TyperCommand.get_params(self, ctx)

    def _visible_real_params(self, ctx: Context) -> list[Parameter]:
        return [
            param
            for param in self._real_params(ctx)
            if not getattr(param, "hidden", False)
        ]

    def _help_agent_argument(self) -> TyperArgument:
        return _HelpOnlyTyperArgument(
            param_decls=["agent"],
            metavar="AGENT",
            type=TextType(),
            required=True,
            default=None,
            expose_value=False,
            help=self.argument_help,
        )

    def get_params(self, ctx: Context) -> list[Parameter]:
        return [self._help_agent_argument(), *self._real_params(ctx)]

    def format_usage(self, ctx: Context, formatter: HelpFormatter) -> None:
        prefix_path = _prefix_usage_path(ctx, self.usage_agent_metavar)
        pieces = [self.options_metavar] if self.options_metavar else []
        for param in self._visible_real_params(ctx):
            pieces.extend(parameter_usage(param, ctx))
        write_usage(formatter, prefix_path, " ".join(pieces))


class RunAgentCommand(RuntimeAgentCommand):
    argument_help = "Agent name, reference, or URL"

    def format_usage(self, ctx: Context, formatter: HelpFormatter) -> None:
        pieces = [self.options_metavar] if self.options_metavar else []
        for param in self._visible_real_params(ctx):
            pieces.extend(parameter_usage(param, ctx))
        pieces.append(self.usage_agent_metavar)
        formatter.write_usage(ctx.command_path, " ".join(pieces))


class StartAgentCommand(RuntimeAgentCommand):
    argument_help = "Existing local agent name"


class OptionalPrefixAgentTemplateCommand(OptionalPrefixAgentCommand):
    def _help_template_argument(self) -> TyperArgument:
        return _HelpOnlyTyperArgument(
            param_decls=["name"],
            metavar="NAME",
            type=TextType(),
            required=False,
            default=None,
            expose_value=False,
            help="Template name",
        )

    def get_params(self, ctx: Context) -> list[Parameter]:
        return [
            self._prefix_agent_argument(),
            self._help_template_argument(),
            *self._real_params(ctx),
        ]


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
