"""Toolang command classes using the shared Typer help formatter."""

from __future__ import annotations

from typing import Any, NoReturn

import typer
from rich.text import Text
from typer._click import (
    Command,
    Context,
    Parameter,
    HelpFormatter as NativeHelpFormatter,
)
from typer._click.exceptions import ClickException, UsageError
from typer._click.shell_completion import CompletionItem
from typer.core import TyperArgument, TyperCommand, TyperGroup

from toolang.common.typer.ui import HelpFormatter, argument_usage
from .agent_argument import (
    AgentParameter,
    agent_parameter,
    agent_parameters,
    agent_prefix,
    install_agent_parameter,
)


def parameter_usage(param: Parameter, ctx: Context) -> list[str]:
    """Keep literal syntax while marking optional and repeated operands."""
    pieces = param.get_usage_pieces(ctx)
    if not pieces or not isinstance(param, TyperArgument):
        return pieces
    return [argument_usage(param)]


def _usage_path(ctx: Context, *, agent_after: bool = False) -> Text:
    """Render the command path with its optional leaf-owned agent prefix."""
    contexts = []
    current: Context | None = ctx
    while current is not None:
        contexts.append(current)
        current = current.parent
    contexts.reverse()
    agents = (
        agent_parameters(ctx.command)
        if ctx.parent is not None and not agent_after
        else ()
    )
    owner = contexts[1] if len(contexts) > 1 else None
    path = Text()
    for current in contexts:
        if path:
            path.append(" ")
        if current is owner and agents:
            metavars = {agent.metavar or "AGENT" for agent in agents}
            metavar = next(iter(metavars)) if len(metavars) == 1 else "AGENT"
            required = all(agent.required for agent in agents)
            prefix = f"<{metavar}>" if required else f"[{metavar}]"
            path.append(f"{prefix} ", style="cli.usage")
        path.append(current.info_name or "", style="cli.command.name")
        if current is not ctx:
            for param in current.command.get_params(current):
                for piece in parameter_usage(param, current):
                    path.append(f" {piece}", style="cli.usage")
    return path


class HelpContext(Context):
    formatter_class = HelpFormatter

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._parameters_parsed = False
        if self.parent is None and kwargs.get("help_option_names") is None:
            self.help_option_names = ["-h", "--help"]
        if self.color is None:
            formatter = self.make_formatter()
            assert isinstance(formatter, HelpFormatter)
            self.color = formatter.console.color_system is not None


def write_usage(formatter: NativeHelpFormatter, path: str | Text, args: str) -> None:
    """Keep virtual operands unstyled in a structured command path."""
    if isinstance(formatter, HelpFormatter):
        formatter.write_usage(path, args)
    else:
        formatter.write_usage(str(path), args)


def show_help(ctx: Context) -> NoReturn:
    """Use the command's help callback, including any active UI renderer."""
    option = ctx.command.get_help_option(ctx)
    if option is not None and option.callback is not None:
        option.callback(ctx, option, True)
    typer.echo(ctx.get_help(), color=ctx.color)
    ctx.exit()


def _format_help(ctx: Context, formatter: NativeHelpFormatter) -> None:
    output = formatter if isinstance(formatter, HelpFormatter) else HelpFormatter()
    output.write_help(ctx)
    if output is not formatter:
        formatter.write(output.getvalue())


class CliCommand(TyperCommand):
    """Keep Typer parsing and render help with the shared formatter."""

    context_class = HelpContext

    def invoke(self, ctx: Context) -> Any:
        from .context import command_environment

        with command_environment(ctx):
            return super().invoke(ctx)

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        install_agent_parameter(self)

    def parse_args(self, ctx: Context, args: list[str]) -> list[str]:
        if isinstance(ctx, HelpContext):
            ctx._parameters_parsed = False
        remaining = super().parse_args(ctx, args)
        if isinstance(ctx, HelpContext):
            ctx._parameters_parsed = True
        return remaining

    def get_params(self, ctx: Context) -> list[Parameter]:
        params = super().get_params(ctx)
        if (
            ctx.resilient_parsing
            and isinstance(ctx, HelpContext)
            and ctx._parameters_parsed
        ):
            return [
                param
                for param in params
                if not isinstance(param, AgentParameter)
                or param.accepts_positional(ctx)
            ]
        return params

    def format_usage(self, ctx: Context, formatter: NativeHelpFormatter) -> None:
        agent = agent_parameter(self)
        write_usage(
            formatter,
            _usage_path(
                ctx, agent_after=agent is not None and agent.contract.both_orders
            ),
            " ".join(self.collect_usage_pieces(ctx)),
        )

    def write_help_sections(self, ctx: Context, formatter: HelpFormatter) -> None:
        """Let subclasses append caller-owned sections."""

    def format_help(self, ctx: Context, formatter: NativeHelpFormatter) -> None:
        _format_help(ctx, formatter)

    def collect_usage_pieces(self, ctx: Context) -> list[str]:
        pieces = []
        if (agent := agent_parameter(self)) is not None and agent.contract.both_orders:
            pieces.append(argument_usage(agent))
        if self.options_metavar:
            pieces.append(self.options_metavar)
        for param in self.get_params(ctx):
            pieces.extend(parameter_usage(param, ctx))
        return pieces


class MetadataCommand(CliCommand):
    """Offline metadata commands that never load user factories or execute code."""

    def invoke(self, ctx: Context) -> Any:
        return TyperCommand.invoke(self, ctx)


class CliGroup(TyperGroup):
    """Apply the same help conventions to command groups."""

    context_class = HelpContext

    def write_help_sections(self, ctx: Context, formatter: HelpFormatter) -> None:
        """Let subclasses append caller-owned sections."""

    def __init__(self, *, subcommand_metavar: str | None = None, **kwargs: Any) -> None:
        super().__init__(
            subcommand_metavar=(
                "<COMMAND> [ARGUMENTS]"
                if subcommand_metavar is None
                else subcommand_metavar
            ),
            **kwargs,
        )
        install_agent_parameter(self)

    def format_usage(self, ctx: Context, formatter: NativeHelpFormatter) -> None:
        write_usage(
            formatter, _usage_path(ctx), " ".join(self.collect_usage_pieces(ctx))
        )

    def shell_complete(self, ctx: Context, incomplete: str) -> list[CompletionItem]:
        items = super().shell_complete(ctx, incomplete)
        if agent_prefix(ctx) is not None:
            items = [
                item
                for item in items
                if item.value not in self.commands
                or agent_parameters(self.commands[item.value])
            ]
        return items

    def resolve_command(
        self, ctx: Context, args: list[str]
    ) -> tuple[str | None, Command | None, list[str]]:
        try:
            name, command, remaining = super().resolve_command(ctx, args)
        except UsageError as exc:
            # Resolution uses plain UsageError; parameter errors have their own types.
            if type(exc) is not UsageError:
                raise
            message = exc.format_message()
            if help_option := self.get_help_option(ctx):
                option = (
                    "--help" if "--help" in help_option.opts else help_option.opts[0]
                )
                message += f"\n\nTry '{ctx.command_path} {option}' for help."
            error = ClickException(message)
            error.exit_code = exc.exit_code
            raise error from exc
        if (
            command is not None
            and agent_prefix(ctx) is not None
            and not agent_parameters(command)
        ):
            if ctx.resilient_parsing:
                return None, None, args
            from .lazy import LazyCommand

            selected = command.load() if isinstance(command, LazyCommand) else command
            with selected.context_class(selected, info_name=name, parent=ctx) as child:
                raise UsageError(
                    f"Command {name!r} does not accept an agent prefix.", child
                )
        return name, command, remaining

    def format_help(self, ctx: Context, formatter: NativeHelpFormatter) -> None:
        _format_help(ctx, formatter)

    def collect_usage_pieces(self, ctx: Context) -> list[str]:
        pieces = [self.options_metavar] if self.options_metavar else []
        for param in self.get_params(ctx):
            pieces.extend(parameter_usage(param, ctx))
        pieces.append(self.subcommand_metavar)
        return pieces
