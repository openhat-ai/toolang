"""Reusable, explicit CLI signatures and their display types."""

from pathlib import Path
from typing import Annotated

import typer
from typer._click import Context, Parameter
from typer._click.types import StringParamType
from typer.models import TyperPath


DEVELOPMENT_WHEEL_HELP = "Use a local Toolang wheel"


class TextType(StringParamType):
    """A native string parameter displayed as TEXT."""

    def get_metavar(self, param: Parameter, ctx: Context) -> str:
        return "TEXT"


class PathType(TyperPath):
    """A native path parameter with a conventional type label."""

    def get_metavar(self, param: Parameter, ctx: Context) -> str:
        return self.name.upper()


class SignatureType(StringParamType):
    """An authored signature includes its type; omit the transport type column."""

    def get_metavar(self, param: Parameter, ctx: Context) -> str:
        return ""


RootOption = Annotated[
    Path | None,
    typer.Option("--root", "-r", metavar="PATH", help="Use a custom Toolang root"),
]
AllowOptions = Annotated[
    list[str] | None,
    typer.Option(
        "--allow",
        metavar="<KEY>=<VALUE>",
        help="Select allowed resources; see 'too help allow'",
    ),
]
LimitOptions = Annotated[
    list[str] | None,
    typer.Option(
        "--limit",
        metavar="<KEY>=<VALUE>",
        help="Set run limits; see 'too help limit'",
    ),
]
DefaultOptions = Annotated[
    list[str] | None,
    typer.Option(
        "--default",
        metavar="<KEY>=<VALUE>",
        help="Set runtime or Chat session defaults; see 'too help default'",
    ),
]
CompactModelOption = Annotated[
    str | None,
    typer.Option(
        "--compact-model",
        metavar="<KEY>=<VALUE>",
        help="Set the compaction model; see 'too help model'",
    ),
]

DriverOption = Annotated[
    str | None,
    typer.Option(
        "--enable",
        metavar="DRIVERS",
        help="Enable only these drivers, replacing defaults: chat,chore,talk or none",
    ),
]
