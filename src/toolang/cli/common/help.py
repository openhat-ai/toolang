"""Conventional CLI metavars for Typer's native help renderer."""

from __future__ import annotations

from typer._click import Context, Parameter
from typer.core import TyperArgument, TyperCommand, TyperGroup


def parameter_usage(param: Parameter, ctx: Context) -> list[str]:
    """Keep literal syntax while marking optional and repeated operands."""
    pieces = param.get_usage_pieces(ctx)
    if not pieces or not isinstance(param, TyperArgument):
        return pieces
    label = param.metavar or (param.name or "")
    repeated = param.nargs != 1
    if repeated and label.endswith("..."):
        label = label[:-3]
    if not param.required and not label.startswith("["):
        label = f"[{label}]"
    if repeated:
        label += "..."
    return [label]


class CliCommand(TyperCommand):
    """Use conventional metavars with Typer parsing and Rich help panels."""

    def collect_usage_pieces(self, ctx: Context) -> list[str]:
        pieces = [self.options_metavar] if self.options_metavar else []
        for param in self.get_params(ctx):
            pieces.extend(parameter_usage(param, ctx))
        return pieces


class CliGroup(TyperGroup):
    """Apply the same help conventions to command groups."""

    def collect_usage_pieces(self, ctx: Context) -> list[str]:
        pieces = [self.options_metavar] if self.options_metavar else []
        for param in self.get_params(ctx):
            pieces.extend(parameter_usage(param, ctx))
        pieces.append(self.subcommand_metavar)
        return pieces
