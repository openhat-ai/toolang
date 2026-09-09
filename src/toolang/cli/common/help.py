"""Toolang command classes using the shared Typer help formatter."""

from __future__ import annotations

from typing import Any

from rich.text import Text
from typer._click import Context, Parameter, HelpFormatter as NativeHelpFormatter
from typer.core import TyperArgument, TyperCommand, TyperGroup

from toolang.common.typer.ui import HelpFormatter


def parameter_usage(param: Parameter, ctx: Context) -> list[str]:
    """Keep literal syntax while marking optional and repeated operands."""
    pieces = param.get_usage_pieces(ctx)
    if not pieces or not isinstance(param, TyperArgument):
        return pieces
    label = param.metavar or (param.name or "")
    repeated = param.nargs != 1
    if repeated and label.endswith("..."):
        label = label[:-3]
    if repeated:
        label += "..."
    if not param.required and not label.startswith("["):
        label = f"[{label}]"
    return [label]


class HelpContext(Context):
    formatter_class = HelpFormatter

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
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


def _format_help(ctx: Context, formatter: NativeHelpFormatter) -> None:
    output = formatter if isinstance(formatter, HelpFormatter) else HelpFormatter()
    output.write_help(ctx)
    if output is not formatter:
        formatter.write(output.getvalue())


class CliCommand(TyperCommand):
    """Keep Typer parsing and render help with the shared formatter."""

    context_class = HelpContext

    def format_help(self, ctx: Context, formatter: NativeHelpFormatter) -> None:
        _format_help(ctx, formatter)

    def collect_usage_pieces(self, ctx: Context) -> list[str]:
        pieces = [self.options_metavar] if self.options_metavar else []
        for param in self.get_params(ctx):
            pieces.extend(parameter_usage(param, ctx))
        return pieces


class CliGroup(TyperGroup):
    """Apply the same help conventions to command groups."""

    context_class = HelpContext

    def __init__(self, *, subcommand_metavar: str | None = None, **kwargs: Any) -> None:
        super().__init__(
            subcommand_metavar=(
                "COMMAND [ARGS]" if subcommand_metavar is None else subcommand_metavar
            ),
            **kwargs,
        )

    def format_help(self, ctx: Context, formatter: NativeHelpFormatter) -> None:
        _format_help(ctx, formatter)

    def collect_usage_pieces(self, ctx: Context) -> list[str]:
        pieces = [self.options_metavar] if self.options_metavar else []
        for param in self.get_params(ctx):
            pieces.extend(parameter_usage(param, ctx))
        pieces.append(self.subcommand_metavar)
        return pieces
