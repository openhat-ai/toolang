"""Agent catalog commands."""

from __future__ import annotations

from toolang.cli.common.workspaces import (
    WorkspaceOptions,
    WorkdirOption,
    NoAutoWorkspaceOption,
    resolve_workspaces,
    running_workspaces,
)

import asyncio
from datetime import UTC, datetime
from pathlib import Path
import shutil
from typing import Annotated, cast

import typer
from typer._click.exceptions import ClickException

from toolang.cli.common.parameters import TextType

from toolang.catalog.job import AuthoredJobs
from toolang.catalog.agent import LocalAgents
from toolang.common.layout import AgentLayout
from toolang.common.time import format_duration
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
    agent_avatar,
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
            metavar="AGENT", click_type=TextType(), help="New local agent name"
        ),
    ],
    template: Annotated[
        str,
        typer.Option("--template", "-t", metavar="NAME", help="Template name"),
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
        raise ClickException(f"Agent {agent} already exists") from exc
    typer.echo(f"Agent {agent} created: {home / 'agent.too'}")


def clone_agent(
    ctx: typer.Context,
    source: Annotated[
        str,
        typer.Argument(
            metavar="SOURCE",
            click_type=TextType(),
            help="Agent name, reference, or URL",
        ),
    ],
    target: Annotated[
        str | None,
        typer.Argument(
            metavar="TARGET", click_type=TextType(), help="New local agent name"
        ),
    ] = None,
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
        typer.Argument(metavar="AGENT", click_type=TextType(), help="Local agent name"),
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
    agent: str | None = typer.Argument(
        None, help="Agent name, .too file, reference, or URL", hidden=True
    ),
    model_catalog: ModelCatalogOption = None,
    workspace: WorkspaceOptions = None,
    workdir: WorkdirOption = None,
    no_auto_workspace: NoAutoWorkspaceOption = False,
) -> None:
    agent_name = require_runtime_agent(ctx, agent)
    selected_layout = cli_context(ctx).layout
    layout = selected_layout or AgentLayout.resident(context_root(ctx), agent_name)
    selection = user_call(
        resolve_workspaces,
        layout,
        procdir=Path.cwd(),
        paths=workspace or (),
        workdir=workdir,
        srcdir=layout.program.resolve().parent
        if layout.placement == "roaming"
        else None,
        no_auto=no_auto_workspace,
        existing=running_workspaces(layout) if workdir else None,
    )
    process = agents.AgentProcess(layout)
    status = user_call(process.status, ui_base_url=ui_base_url())
    if status is None:
        raise ClickException(f"Agent {agent_name} not found")
    try:
        runtime_state = process.state() or {}
        runtime_identity = agents.runtime_identity_row(runtime_state, layout=layout)
    except (OSError, ValueError):
        runtime_state, runtime_identity = {}, None
    state = _prepare_state(layout)
    model_catalog = resolve_model_catalog_option(model_catalog)
    watcher = (
        SetupWatcher(layout, model_catalog=model_catalog)
        if model_catalog is not None
        else SetupWatcher(layout)
    )
    setup = asyncio.run(watcher.refresh())
    started_at = runtime_value(runtime_state.get("started_at"))
    status_value = "not running" if status.status == "stopped" else status.status
    if status.status == "running" and started_at != "-":
        online = _human_uptime_since(started_at)
        if online is not None:
            status_value = f"{status.status} ({online})"
    message = runtime_value(status.message)
    if status.status not in {"running", "stopped"} and message != "-":
        status_value = f"{status_value}: {message}"
    workspace_grants = {**state.workspaces, **selection.additions}
    runtime_workspaces = runtime_state.get("workspace_additions")
    if status.status == "running" and isinstance(runtime_workspaces, dict):
        workspace_grants.update(
            (name, path)
            for name, path in runtime_workspaces.items()
            if isinstance(name, str) and isinstance(path, str)
        )
    rows = [
        ("Home", shorten_home_path(layout.home)),
        ("Tools", _tools_summary(setup)),
        ("Models", _models_summary(setup)),
        ("Caps", _caps_summary(state)),
        ("Jobs", _jobs_summary(layout)),
        ("Workspaces", ", ".join(setup.workspace_grants(workspace_grants))),
        ("Status", status_value),
    ]
    if status.status == "stopped":
        echo_pairs_table(rows, avatar=agent_avatar(), title=agent_name.upper())
        return
    if status.sandbox:
        rows.append(("Sandbox", status.sandbox))
    if runtime_identity is not None and status.status != "stopped":
        rows.append(runtime_identity)
    if status.endpoint:
        rows.append(("API", status.endpoint))
    if status.webui_url:
        rows.append(("WebUI", status.webui_url))
    echo_pairs_table(rows, avatar=agent_avatar(), title=agent_name.upper())


def _caps_summary(state: AgentState) -> str:
    caps = state.caps_for("agent")
    counts = {
        "psyches": sum(item.kind == "psyche" for item in caps),
        "skills": sum(item.kind == "skill" for item in caps),
        "services": sum(item.kind == "service" for item in caps),
        "prompts": sum(item.kind == "prompt" for item in caps),
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
    progress = make_cli_progress()
    try:
        with progress:
            state = cast(
                AgentState,
                user_call(
                    prepare_agent_state,
                    layout,
                    progress=progress.sink,
                ),
            )
            return state
    except Exception as exc:
        if progress.failure_stage is not None:
            raise ClickException(progress.failure_message(exc)) from exc
        raise


def _jobs_summary(layout: AgentLayout) -> str:
    catalog = AuthoredJobs(layout.home)
    chore_count = len(catalog.list(kind="chore"))
    task_count = len(catalog.list(kind="task"))
    return (
        f"{chore_count} {'chore' if chore_count == 1 else 'chores'}, "
        f"{task_count} {'task' if task_count == 1 else 'tasks'}"
    )


def _models_summary(setup: AgentSetup) -> str:
    model_count = len(setup.models_effective())
    provider_count = len(setup.providers_effective())
    return (
        f"{model_count} {'model' if model_count == 1 else 'models'}, "
        f"{provider_count} {'provider' if provider_count == 1 else 'providers'}"
    )


def _tools_summary(setup: AgentSetup) -> str:
    tools = setup.tools()
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
