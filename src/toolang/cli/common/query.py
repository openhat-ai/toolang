"""Shared CLI adapters for collection-query discovery and errors."""

from __future__ import annotations

import click
from typing import Any

from toolang.common.errors import ToolangError
from toolang.common.query import QueryDataset


def query_items(
    dataset: QueryDataset[Any],
    queries: list[str] | tuple[str, ...] | None,
) -> tuple[Any, ...]:
    """Evaluate CLI query values with a consistent user-facing error."""

    try:
        return dataset.query(queries or None)
    except ToolangError as error:
        raise click.ClickException(str(error)) from error


__all__ = ["query_items"]
