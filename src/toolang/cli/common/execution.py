"""Local execution resources owned by one CLI command."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator

import click
import typer

from toolang.common.ids import IdIssuer
from toolang.execution.store import RunStore

from .context import context_layout


@dataclass(frozen=True, slots=True)
class ExecutionResources:
    """Process-local access to one agent's durable execution state."""

    store: RunStore
    ids: IdIssuer


@contextmanager
def open_execution(
    ctx: typer.Context,
    *,
    required: bool = False,
) -> Iterator[ExecutionResources | None]:
    """Open one agent's execution store without creating it for read-only access."""

    layout = context_layout(ctx)
    if not layout.run_store.is_file():
        if required:
            raise click.ClickException(
                f"execution history not found: {layout.name}"
            )
        yield None
        return
    store = RunStore(layout.run_store)
    try:
        yield ExecutionResources(store=store, ids=IdIssuer(layout.id_state))
    finally:
        store.close()
