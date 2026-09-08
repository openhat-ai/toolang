"""Reusable, explicit CLI signatures and their display types."""

from pathlib import Path
from typing import Annotated

import typer
from typer._click import Context, Parameter
from typer._click.types import StringParamType
from typer.models import TyperPath


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
    typer.Option("--root", "-r", metavar="PATH", help="Use a custom Toolang root."),
]
AllowOptions = Annotated[
    list[str] | None,
    typer.Option(
        "--allow",
        metavar="RESOURCE=QUERY",
        help="Set RESOURCE=QUERY. Repeat by resource category.",
    ),
]
LimitOptions = Annotated[
    list[str] | None,
    typer.Option(
        "--limit",
        metavar="LIMIT=VALUE",
        help="Set a run limit, e.g. tokens=10000. Repeat for another limit.",
    ),
]
DefaultOptions = Annotated[
    list[str] | None,
    typer.Option(
        "--default",
        metavar="SETTING=VALUE",
        help="Set a default model or runnable. Repeat for another setting.",
    ),
]
CompactionModelOption = Annotated[
    str | None,
    typer.Option(
        "--compaction-model",
        metavar="MODEL_SPEC",
        help=(
            "Set an exact model (optional effort=LEVEL), or unset, for a new runtime."
        ),
    ),
]
