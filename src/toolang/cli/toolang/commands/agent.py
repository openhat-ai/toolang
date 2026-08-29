"""Agent catalog commands."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
import shutil
from typing import Annotated, cast

import click
import typer

from toolang.catalog.job import AuthoredJobs
from toolang.catalog.agent import LocalAgents
from toolang.common.layout import AgentLayout
from toolang.up import process as agents
from toolang.catalog import templates
from toolang.setup import AgentSetup, SetupWatcher
from toolang.state.prepare import prepare_agent_state
from toolang.state.state import AgentState
from ...common.context import (
    ModelCatalogOption,
    cli_context,
    context_root,
    require_runtime_agent,
    resolve_model_catalog_option,
    ui_base_url,
    user_call,
)
from ...common.output import (
    active_agent_error,
    agent_avatar,
    created_time,
    echo_pairs_table,
    echo_table,
    parse_utc_timestamp,
    runtime_value,
    shorten_home_path,
)
from ...common.progress import as_progress_sink, make_cli_progress
from . import plugin


def new_agent(
    ctx: typer.Context,
    agent: Annotated[str, typer.Argument(help="Agent name")],
    template: Annotated[
        str,
        typer.Option("--template", "-t", help="Template name."),
    ] = "default",
) -> None:
    root = context_root(ctx)
    try:
        source_text = templates.render_template(
            "agent",
            template,
            agent_name=agent,
            name=agent,
        )
        home = LocalAgents(root / "agents").create(agent, content=source_text)
    except FileExistsError as exc:
        raise click.ClickException(f"Agent {agent} already exists") from exc
    typer.echo(f"Created agent {agent}: {home / 'agent.too'}")


def clone_agent(
    ctx: typer.Context,
    source: Annotated[str, typer.Argument(help="Agent source selector.")],
    target: Annotated[str | None, typer.Argument(help="New local agent name.")] = None,
) -> None:
    root = context_root(ctx)
    try:
        homes = LocalAgents(root / "agents")
        selector = agents.parse_agent_selector(source)
        if selector.form == "name":
            if target is None:
                raise ValueError("target name is required when cloning one local agent")
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
            ref = agents.resolve_agent_selector_ref(selector)
            name = target or selector.default_name()
            home = homes.create(name, content=agents.fetch_agent_ref(ref))
    except FileExistsError as exc:
        target_name = target or Path(source).stem
        raise click.ClickException(f"Agent {target_name} already exists") from exc
    except FileNotFoundError as exc:
        raise click.ClickException(f"Agent {source} not found") from exc
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    typer.echo(f"Cloned agent {home.name}: {home / 'agent.too'}")


def remove_agent(
    ctx: typer.Context,
    agent: Annotated[str, typer.Argument(help="Agent name")],
) -> None:
    from toolang.up import sandbox as sandbox_runtime

    root = context_root(ctx)
    layout = AgentLayout.resident(root, agent)
    process = agents.AgentProcess(layout)
    status = process.status(ui_base_url=ui_base_url())
    if status is not None and status.status in {"running", "preparing", "starting"}:
        raise click.ClickException(active_agent_error(status))
    if process.pids():
        raise click.ClickException(f"Agent {agent} already running")
    try:
        user_call(asyncio.run, sandbox_runtime.release_for_removal(layout))
        LocalAgents(root / "agents").remove(agent)
    except FileNotFoundError as exc:
        raise click.ClickException(f"Agent {agent} not found") from exc
    except (OSError, RuntimeError) as exc:
        raise click.ClickException(f"Could not release agent {agent}: {exc}") from exc
    typer.echo(f"Removed agent {agent}")


def list_agents(ctx: typer.Context) -> None:
    items = agents.AgentProcess.list(
        context_root(ctx),
        ui_base_url=ui_base_url(),
    )
    if not items:
        typer.echo("No agents found.")
        return
    rows = [
        (
            item.name,
            item.status,
            item.sandbox if item.status == "running" and item.sandbox else "-",
            str(item.port) if item.port is not None else "-",
            item.webui_url or "-",
        )
        for item in items
    ]
    echo_table(("AGENT", "STATUS", "SANDBOX", "PORT", "WEBUI"), rows)


def info_agent(
    ctx: typer.Context,
    agent: str | None = typer.Argument(None, help="Agent name", hidden=True),
    model_catalog: ModelCatalogOption = None,
) -> None:
    agent_name = require_runtime_agent(ctx, agent)
    selected_layout = cli_context(ctx).layout
    layout = selected_layout or AgentLayout.resident(context_root(ctx), agent_name)
    process = agents.AgentProcess(layout)
    status = user_call(process.status, ui_base_url=ui_base_url())
    if status is None:
        raise click.ClickException(f"Agent {agent_name} not found")
    runtime_state = process.state() or {}
    state = _prepare_state(layout)
    model_catalog = resolve_model_catalog_option(model_catalog)
    watcher = (
        SetupWatcher(layout, model_catalog=model_catalog)
        if model_catalog is not None
        else SetupWatcher(layout)
    )
    setup = asyncio.run(watcher.refresh())
    created_at = created_time(layout.home)
    started_at = runtime_value(runtime_state.get("started_at"))
    updated_at = runtime_value(runtime_state.get("updated_at"))
    status_value = status.status
    if status.status == "running" and started_at != "-":
        online = _human_uptime_since(started_at)
        if online is not None:
            status_value = f"{status.status} ({online})"
    rows = [
        ("Home", shorten_home_path(layout.home)),
        ("Caps", _caps_summary(state)),
        ("Jobs", _jobs_summary(layout)),
        ("Tools", _tools_summary(setup)),
        (
            "Models",
            _models_summary(
                state,
                setup,
                runtime_state=runtime_state,
                running=status.status != "stopped",
            ),
        ),
        ("Status", status_value),
    ]
    if status.status == "stopped":
        rows.append(("Created", created_at))
        echo_pairs_table(rows, avatar=agent_avatar(), title=agent_name.upper())
        return
    if status.sandbox:
        rows.append(("Sandbox", status.sandbox))
    message = runtime_value(runtime_state.get("message"))
    runtime_identity = agents.runtime_identity_row(runtime_state, layout=layout)
    if runtime_identity is not None and status.status != "stopped":
        rows.append(runtime_identity)
    if status.endpoint:
        rows.append(("API", status.endpoint))
    if status.webui_url:
        rows.append(("WebUI", status.webui_url))
    if status.status == "running" and started_at != "-":
        rows.append(("Started", started_at))
    if status.status != "running" and updated_at != "-":
        rows.append(("Updated", updated_at))
    if status.status != "running" and message != "-":
        rows.append(("Message", message))
    echo_pairs_table(rows, avatar=agent_avatar(), title=agent_name.upper())


def _caps_summary(state: AgentState) -> str:
    counts = {
        "psyches": len(state.psyches),
        "skills": len(state.skills),
        "services": len(state.services),
        "prompts": len(state.prompts),
    }
    singular = {
        "psyches": "psyche",
        "skills": "skill",
        "services": "service",
        "prompts": "prompt",
    }
    return ", ".join(
        f"{count} {singular[label] if count == 1 else label}"
        for label, count in counts.items()
    )


def _prepare_state(layout: AgentLayout) -> AgentState:
    progress = make_cli_progress(show_materialize_summary=True)
    try:
        return cast(
            AgentState,
            user_call(
                prepare_agent_state,
                layout,
                progress=as_progress_sink(progress),
            ),
        )
    finally:
        progress.finish(details=False)


def _jobs_summary(layout: AgentLayout) -> str:
    catalog = AuthoredJobs(layout.home)
    chore_count = len(catalog.list(kind="chore"))
    task_count = len(catalog.list(kind="task"))
    return (
        f"{chore_count} {'chore' if chore_count == 1 else 'chores'}, "
        f"{task_count} {'task' if task_count == 1 else 'tasks'}"
    )


def _models_summary(
    state: AgentState,
    setup: AgentSetup,
    *,
    runtime_state: dict[str, object],
    running: bool,
) -> str:
    from toolang.plugin.models.config import parse_default_models

    selectors: Sequence[str] = ()
    raw_models = runtime_state.get("models")
    if running and isinstance(raw_models, list):
        selectors = tuple(
            value.strip()
            for item in raw_models
            if isinstance(item, str) and (value := item.strip())
        )
    if not selectors:
        selectors = parse_default_models(
            (state.root_config, state.home_config),
        )
    rows = plugin.model_rows(
        setup,
        config_layers=(state.root_config, state.home_config),
        model_selectors=selectors,
    )
    provider_count = len({provider for _model, provider, _detail in rows})
    return (
        f"{len(rows)} {'model' if len(rows) == 1 else 'models'}, "
        f"{provider_count} {'provider' if provider_count == 1 else 'providers'}"
    )


def _tools_summary(setup: AgentSetup) -> str:
    rows = plugin.tool_rows(setup)
    set_count = len({toolset for toolset, _tool, _description in rows})
    return (
        f"{len(rows)} {'tool' if len(rows) == 1 else 'tools'}, "
        f"{set_count} {'set' if set_count == 1 else 'sets'}"
    )


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _human_uptime_since(timestamp_text: str) -> str | None:
    started = parse_utc_timestamp(timestamp_text)
    if started is None:
        return None
    import humanize

    total_seconds = max(int((_utc_now() - started).total_seconds()), 0)
    return f"up {humanize.naturaldelta(total_seconds)}"
