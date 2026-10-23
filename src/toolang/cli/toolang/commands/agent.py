"""Agent catalog commands."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
import shutil
from typing import Annotated, cast

import typer

from toolang.cli.common.agent_argument import AgentArgument, AgentNameType
from typer._click.exceptions import ClickException

from toolang.cli.common.parameters import TextType

from toolang.catalog.agent import LocalAgents
from toolang.common.layout import AgentLayout, validate_agent_name
from toolang.base.errors import ToolangError
from toolang.common.progress import ProgressSink
from toolang.common.time import format_duration
from toolang.up import source as agent_sources
from toolang.up import process as agents
from toolang.up.config import format_drivers
from toolang.up.watcher import runtime_status
from toolang.up.types import ALL_DRIVERS, ExecutionDrivers
from toolang.catalog import templates
from toolang.setup import AgentSetup
from toolang.state.state import AgentState
from ...common.context import (
    ModelCatalogOption,
    cli_context,
    context_root,
    context_layout,
    require_runtime_agent,
    resolve_model_catalog_option,
    ui_base_url,
    user_call,
)
from ...common.output import (
    agent_avatar,
    echo_collection_summary,
    echo_pairs_table,
    echo_table,
    parse_utc_timestamp,
    runtime_value,
    shorten_home_path,
)
from ...common.progress import make_cli_progress


def new_agent(
    ctx: typer.Context,
    agent: Annotated[
        str,
        typer.Argument(
            metavar="NAME",
            click_type=AgentNameType(),
            help="New agent name",
        ),
    ],
) -> None:
    root = context_root(ctx)
    try:
        source_text = templates.load_template("agent").raw_text
        home = LocalAgents(root / "agents").create(agent, content=source_text)
    except FileExistsError as exc:
        raise ClickException(f"Agent {agent} already exists") from exc
    except ValueError as exc:
        raise ClickException(str(exc)) from exc
    typer.echo(f"Agent {agent} created: {home / 'agent.too'}")


def clone_agent(
    ctx: typer.Context,
    source: Annotated[
        str,
        typer.Argument(
            metavar="SOURCE",
            click_type=TextType(),
            help="Agent name, ref or URL",
        ),
    ],
    target: Annotated[
        str | None,
        typer.Argument(
            metavar="NAME",
            click_type=AgentNameType(),
            help="New agent name; required for resident agents",
        ),
    ] = None,
) -> None:
    root = context_root(ctx)
    try:
        homes = LocalAgents(root / "agents")
        if target is not None:
            validate_agent_name(target)
        selector = agent_sources.parse_agent_target(source)
        if selector.form == "name":
            if target is None:
                raise ValueError("a new name is required when cloning a resident agent")
            source_home = homes.get(selector.name or "")
            if source_home is None:
                raise FileNotFoundError(source)
            home = homes.path(target)
            if home.exists():
                raise FileExistsError(home)
            shutil.copytree(
                source_home,
                home,
                ignore=shutil.ignore_patterns(".caps", ".state", ".runtime"),
            )
        else:
            name = target or selector.default_name()
            home = homes.path(name)
            if home.exists():
                raise FileExistsError(home)
            ref = agent_sources.resolve_agent_source(selector)
            home = homes.create(name, content=agent_sources.fetch_agent_ref(ref))
    except FileExistsError as exc:
        target_name = target or Path(source).stem
        raise ClickException(f"Agent {target_name} already exists") from exc
    except FileNotFoundError as exc:
        raise ClickException(f"Agent {source} not found") from exc
    except ValueError as exc:
        raise ClickException(str(exc)) from exc
    typer.echo(f"Agent {home.name} cloned: {home / 'agent.too'}")


def remove_agent(
    ctx: typer.Context,
    agent: Annotated[
        str,
        typer.Argument(metavar="NAME", click_type=AgentNameType(), help="Agent name"),
    ],
) -> None:
    from toolang.up import sandbox as sandbox_runtime

    root = context_root(ctx)
    layout = AgentLayout.resident(root, agent)
    try:
        user_call(asyncio.run, sandbox_runtime.remove_agent(layout))
    except FileNotFoundError as exc:
        raise ClickException(f"Agent {agent} not found") from exc
    except (OSError, RuntimeError) as exc:
        raise ClickException(f"Could not release agent {agent}: {exc}") from exc
    typer.echo(f"Agent {agent} removed")


def list_agents(ctx: typer.Context) -> None:
    items = agents.AgentProcess.list(
        context_root(ctx),
        ui_base_url=ui_base_url(),
    )
    if not items:
        echo_collection_summary(0, "agent")
        return
    rows = [
        (
            item.name,
            "not running" if item.status == "stopped" else item.status,
            item.sandbox if item.status == "running" and item.sandbox else "-",
            str(item.port) if item.port is not None else "-",
            item.webui_url or "-",
        )
        for item in items
    ]
    echo_table(
        ("AGENT", "STATUS", "SANDBOX", "PORT", "WEBUI"),
        rows,
        justify=("left", "left", "left", "right", "left"),
    )
    echo_collection_summary(len(items), "agent")


def info_agent(
    ctx: typer.Context,
    agent: Annotated[str, AgentArgument("home", both_orders=True)],
    model_catalog: ModelCatalogOption = None,
) -> None:
    require_runtime_agent(ctx, agent)
    layout = context_layout(ctx)
    resources = []
    preparation_error = None
    try:
        with make_cli_progress(preparation=True) as progress:
            layout = _prepare_info_layout(ctx, progress=progress.sink)
            resources, preparation_error = _local_resources(
                layout,
                model_catalog=resolve_model_catalog_option(model_catalog),
                progress=progress.sink,
            )
            progress.complete_preparation()
    except (OSError, RuntimeError, ToolangError, ValueError, ClickException) as exc:
        preparation_error = str(exc)
        layout = context_layout(ctx)
    status = user_call(runtime_status, layout, ui_base_url=ui_base_url())
    if status is None:
        if not preparation_error:
            raise ClickException(f"Agent {layout.name} not found")
        from toolang.up.types import AgentStatus

        status = AgentStatus(layout.name, "unknown", None, None, None, None)
    entry = status.entry or {}
    runtime_state = {**entry.get("control", {}), **entry.get("workload", {})}
    runtime_identity = agents.runtime_identity_row(runtime_state)
    started_at = runtime_value(runtime_state.get("started_at"))
    status_value = "not running" if status.status == "stopped" else status.status
    if status.status == "running" and started_at != "-":
        online = _human_uptime_since(started_at)
        if online is not None:
            status_value = f"{status.status} ({online})"
    message = runtime_value(status.message)
    if status.status not in {"running", "stopped"} and message != "-":
        status_value = f"{status_value}: {message}"
    rows = [
        ("Home", shorten_home_path(layout.home)),
        *resources,
        ("Status", status_value),
    ]
    if status.status == "running":
        rows.append(("Drivers", _drivers_summary(runtime_state.get("drivers"))))
    if status.status != "stopped":
        if status.sandbox:
            rows.append(("Sandbox", status.sandbox))
        if runtime_identity is not None:
            rows.append(runtime_identity)
        if status.endpoint:
            rows.append(("API", status.endpoint))
        if status.webui_url:
            rows.append(("WebUI", status.webui_url))
    if preparation_error:
        rows.append(("Preparation", preparation_error))
    title = layout.name.upper()
    if layout.place_code:
        title += f"@{layout.place_code}"
    echo_pairs_table(rows, avatar=agent_avatar(), title=title)
    if preparation_error:
        raise typer.Exit(1)


def _drivers_summary(value: object) -> str:
    if not isinstance(value, list) or any(
        not isinstance(driver, str) or driver not in ALL_DRIVERS for driver in value
    ):
        return "-"
    drivers = frozenset(value)
    if len(drivers) != len(value):
        return "-"
    return format_drivers(cast(ExecutionDrivers, drivers))


def _caps_summary(state: AgentState) -> str:
    caps = state.caps_for("agent")
    counts = {
        "psyches": sum(item.kind == "psyche" for item in caps),
        "skills": sum(item.kind == "skill" for item in caps),
        "services": sum(item.kind == "service" for item in caps),
        "prompts": sum(item.kind == "prompt" for item in caps),
    }
    return _cap_counts_summary(counts)


def _prepare_info_layout(
    ctx: typer.Context, *, progress: ProgressSink | None
) -> AgentLayout:
    """Prepare one selected home without allocating or registering an execution."""
    from dataclasses import replace
    from toolang.up.allocation import retained_roaming

    state = cli_context(ctx)
    layout = context_layout(ctx)
    if layout.placement == "resident":
        if not layout.home.is_dir():
            raise ClickException(f"Agent {layout.name} not found")
    elif state.source is not None:
        if not state.instance_selected and layout.place_code is None:
            retained = retained_roaming(state.source, toolang_root=state.root)
            if retained:
                layout = retained[0]
        state.layout = layout
        layout = replace(
            agent_sources.materialize_roaming_program(state.source, layout=layout),
            root=state.root,
        )
    else:
        layout = agent_sources.prepare_visiting_layout(
            layout,
            toolang_root=state.root,
            source_ref=state.selector,
            progress=progress,
        )
    state.layout = layout
    state.prepared = True
    return layout


def _local_resources(
    layout: AgentLayout,
    *,
    model_catalog: Path | None,
    progress: ProgressSink | None = None,
) -> tuple[list[tuple[str, str]], str | None]:
    from toolang.setup import SetupWatcher
    from toolang.state.watcher import StateWatcher

    async def inspect():
        setup_watcher = SetupWatcher(
            layout, model_catalog=model_catalog, validate_defaults=False
        )
        state_watcher = StateWatcher(layout)
        setup = await setup_watcher.refresh(progress=progress)
        state = await state_watcher.refresh(progress=progress)
        rows = [
            ("Caps", _caps_summary(state)),
            ("Tools", _tools_summary(setup, progress=progress)),
            ("Models", _models_summary(setup, progress=progress)),
            ("Chores", _count(len(state.chores), "chore")),
            ("Workspaces", ", ".join(setup.workspace_grants(state.workspaces)) or "-"),
        ]
        diagnostics = (*setup_watcher.diagnostics(), *state_watcher.diagnostics())
        error = (
            "Last valid state: " + "; ".join(item.message for item in diagnostics)
            if diagnostics
            else None
        )
        return rows, error

    return asyncio.run(inspect())


def _count(value: int, singular: str) -> str:
    return f"{value} {singular if value == 1 else singular + 's'}"


def _cap_counts_summary(counts: Mapping[str, int]) -> str:
    return ", ".join(
        _count(counts[kind + "s"], kind)
        for kind in ("psyche", "skill", "service", "prompt")
    )


def _models_summary(setup: AgentSetup, *, progress: ProgressSink | None = None) -> str:
    model_count = len(setup.models_effective(progress=progress))
    provider_count = len(setup.providers_effective(progress=progress))
    return (
        f"{model_count} {'model' if model_count == 1 else 'models'}, "
        f"{provider_count} {'provider' if provider_count == 1 else 'providers'}"
    )


def _tools_summary(setup: AgentSetup, *, progress: ProgressSink | None = None) -> str:
    tools = setup.tools(progress=progress)
    set_count = len({ref.partition("/")[0] for ref in tools.refs()})
    return (
        f"{len(tools)} {'tool' if len(tools) == 1 else 'tools'}, "
        f"{set_count} {'toolset' if set_count == 1 else 'toolsets'}"
    )


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _human_uptime_since(timestamp_text: str) -> str | None:
    started = parse_utc_timestamp(timestamp_text)
    if started is None:
        return None
    total_seconds = max(int((_utc_now() - started).total_seconds()), 0)
    return f"up {format_duration(total_seconds)}"
