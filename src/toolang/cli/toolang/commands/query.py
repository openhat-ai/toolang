"""Data-independent collection-query help."""

from __future__ import annotations

from typing import Annotated, Any

import click
import typer

from toolang.common.query import CollectionSchema
from toolang.plugin.models.collections import MODEL_SCHEMA
from toolang.plugin.toolsets.collections import TOOL_SCHEMA
from toolang.state.collections import cap_kind_definition
from .metadata import QUERY_HELP

COLLECTIONS = ("models", "tools", "psyches", "skills", "services", "prompts")


def query_command(
    collection: Annotated[
        str | None,
        typer.Argument(help="Base collection whose query fields to show."),
    ] = None,
    json_: Annotated[
        bool,
        typer.Option("--json", help="Write the query schema as JSON."),
    ] = False,
) -> None:
    """Show generic or collection-specific query help."""

    if collection is None:
        if json_:
            raise click.UsageError("--json requires COLLECTION")
        typer.echo(QUERY_HELP.strip())
        return
    schemas = _schemas()
    schema = schemas.get(collection)
    if schema is None:
        supported = ", ".join(COLLECTIONS)
        raise click.BadParameter(
            f"unknown query collection {collection!r}; supported: {supported}",
            param_hint="COLLECTION",
        )
    typer.echo(schema.to_json() if json_ else schema.help_text())


def _schemas() -> dict[str, CollectionSchema[Any]]:
    return {
        "models": MODEL_SCHEMA,
        "tools": TOOL_SCHEMA,
        "psyches": cap_kind_definition("psyche").schema,
        "skills": cap_kind_definition("skill").schema,
        "services": cap_kind_definition("service").schema,
        "prompts": cap_kind_definition("prompt").schema,
    }


__all__ = ["COLLECTIONS", "QUERY_HELP", "query_command"]
