"""Local execution resources owned by one CLI command."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator

import click
import typer

from toolang.common.ids import IdIssuer
from toolang.execution.errors import RunStoreSchemaError
from toolang.execution.store import RunStore

from .context import context_layout
from .version import toolang_version


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
    writable: bool = False,
) -> Iterator[ExecutionResources | None]:
    """Open one agent's execution store in the requested access mode."""

    layout = context_layout(ctx)
    if not layout.run_store.is_file():
        if required:
            raise click.ClickException(
                f"execution history not found: {layout.name}"
            )
        yield None
        return
    try:
        store = RunStore(layout.run_store, read_only=not writable)
    except RunStoreSchemaError as exc:
        raise click.ClickException(
            run_store_schema_error(exc, path=layout.run_store)
        ) from exc
    try:
        yield ExecutionResources(store=store, ids=IdIssuer(layout.id_state))
    finally:
        store.close()


def run_store_schema_error(error: RunStoreSchemaError, *, path: object) -> str:
    """Render one actionable CLI diagnostic for an incompatible run store."""

    if error.version > error.current:
        advice = (
            "Upgrade this CLI to a Toolang version that supports the newer schema."
        )
    elif error.version in error.supported:
        advice = (
            "Start this agent once with the current Toolang runtime to apply the "
            "supported upgrade, then retry."
        )
    else:
        advice = (
            "Restore a compatible backup or migrate it with a Toolang version that "
            f"supports schema {error.version}."
        )
    return (
        f"execution history is incompatible with toolang {toolang_version()}: "
        f"{path} uses schema {error.version}, while this build requires schema "
        f"{error.current}. {advice} The database was not changed."
    )
