"""Shared CLI adapters for collection-query discovery and errors."""

from __future__ import annotations

import click
import typer
from typing import Any

from toolang.common.errors import ToolangError
from toolang.common.query import CollectionSchema, QueryDataset


def emit_query_discovery(
    schema: CollectionSchema[Any],
    *,
    query_help: bool,
    query_schema: bool,
) -> bool:
    """Write requested query discovery and return whether command work is done."""

    if query_help:
        typer.echo(schema.help_text())
    if query_schema:
        typer.echo(schema.to_json())
    return query_help or query_schema


def query_items(
    dataset: QueryDataset[Any],
    queries: list[str] | tuple[str, ...] | None,
) -> tuple[Any, ...]:
    """Evaluate CLI query values with a consistent user-facing error."""

    try:
        return dataset.query(queries or None)
    except ToolangError as error:
        raise click.ClickException(str(error)) from error


__all__ = ["emit_query_discovery", "query_items"]
