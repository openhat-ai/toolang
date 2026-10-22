"""Local execution resources owned by one CLI command."""

from __future__ import annotations

from contextlib import closing, contextmanager
from dataclasses import dataclass
from typing import Iterator

import typer
from typer._click.exceptions import ClickException

from toolang.common.ids import IdIssuer
from toolang.common.layout import AgentLayout
from toolang.common.version import displayed_toolang_version
from toolang.execution.errors import RunStoreSchemaError
from toolang.execution.store import RunStore

from .context import cli_context, context_layout, user_call


@dataclass(frozen=True, slots=True)
class ExecutionResources:
    """Process-local access to one agent's durable execution state."""

    store: RunStore
    ids: IdIssuer


def select_agent_instance(
    ctx: typer.Context, *, record: str | None = None
) -> AgentLayout:
    """Bind one existing home; callers supply their concrete history selector."""
    from toolang.up.identity import read_home_identity, retained_roaming, select_roaming

    state = cli_context(ctx)
    layout = context_layout(ctx)
    if state.instance_selected:
        return layout
    if layout.placement == "resident" and not layout.home.is_dir():
        raise ClickException(f"Agent {layout.name} not found")
    if state.source is not None and layout.place_code is None:
        if record:
            matches = []
            for candidate in user_call(
                retained_roaming, state.source, toolang_root=state.root
            ):
                if not candidate.run_store.is_file():
                    continue
                with closing(_open_store(candidate, read_only=True)) as store:
                    if (
                        store.get_run(run_id=record) is not None
                        or store.get_thread(thread_id=record) is not None
                    ):
                        matches.append(candidate)
            if not matches:
                raise ClickException(
                    f"record not found in retained instances: {record}"
                )
            if len(matches) != 1:
                raise ClickException(
                    "record matches multiple instances; specify name@ID or file.too@ID"
                )
            layout = matches[0]
        else:
            layout = user_call(select_roaming, state.source, toolang_root=state.root)
    entry = user_call(read_home_identity, layout, toolang_root=state.root)
    state.layout = layout
    state.launch_id = entry["launch_id"] if entry else None
    state.instance_selected = True
    return layout


def _open_store(layout: AgentLayout, *, read_only: bool) -> RunStore:
    try:
        return RunStore(layout.run_store, read_only=read_only)
    except RunStoreSchemaError as exc:
        raise ClickException(
            run_store_schema_error(exc, path=layout.run_store)
        ) from exc


@contextmanager
def open_execution(
    ctx: typer.Context,
    *,
    required: bool = False,
    writable: bool = False,
    record: str | None = None,
) -> Iterator[ExecutionResources | None]:
    """Open one agent's execution store in the requested access mode."""

    layout = select_agent_instance(ctx, record=record)
    if not layout.run_store.is_file():
        if required:
            raise ClickException(f"execution history not found: {layout.name}")
        yield None
        return
    store = _open_store(layout, read_only=not writable)
    try:
        yield ExecutionResources(
            store=store, ids=IdIssuer(layout.run_store, agent_name=layout.name)
        )
    finally:
        store.close()


def run_store_schema_error(error: RunStoreSchemaError, *, path: object) -> str:
    """Render one actionable CLI diagnostic for an incompatible run store."""

    if error.version > error.current:
        advice = "Upgrade this CLI to a Toolang version that supports the newer schema."
    else:
        advice = (
            "This build does not migrate older execution history. Preserve or remove "
            "the old store, then create new execution history."
        )
    return (
        f"execution history is incompatible with toolang {displayed_toolang_version()}: "
        f"{path} uses schema {error.version}, while this build requires schema "
        f"{error.current}. {advice} The database was not changed."
    )
