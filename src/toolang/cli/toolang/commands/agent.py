"""Agent catalog commands."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
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
from toolang.common.progress import ProgressSink
from toolang.common.time import format_duration
from toolang.up import process as agents
from toolang.catalog import templates
from toolang.setup import AgentSetup, SetupWatcher
from toolang.state.prepare import prepare_agent_state
from toolang.state.state import AgentState
from toolang.state.schemas import WorkspaceInspection
from ...common.client import RuntimeClient
from ...common.errors import RuntimeClientError
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
) -> None:
    agent_name = require_runtime_agent(ctx, agent)
    selected_layout = cli_context(ctx).layout
    layout = selected_layout or AgentLayout.resident(context_root(ctx), agent_name)
    agent_name = layout.name
    process = agents.AgentProcess(layout)
    status = user_call(process.status, ui_base_url=ui_base_url())
    if status is None and layout.placement == "resident":
        raise ClickException(f"Agent {agent_name} not found")
    try:
        runtime_state = process.state() or {}
        runtime_identity = agents.runtime_identity_row(runtime_state, layout=layout)
    except (OSError, ValueError):
        runtime_state, runtime_identity = {}, None
    if status is not None and status.status == "running":
        if model_catalog is not None:
            raise ClickException("--catalog only applies when the agent is not running")
        if status.endpoint is None:
            raise ClickException("running agent has no endpoint")
        try:
            with make_cli_progress() as progress:
                resources = _running_resources(
                    RuntimeClient(status.endpoint), progress=progress.sink
                )
        except (OSError, RuntimeClientError, ValueError) as exc:
            raise ClickException(str(exc)) from exc
    else:
        resources = _local_resources(
            layout,
            model_catalog=resolve_model_catalog_option(model_catalog),
            source=cli_context(ctx).source,
            selector=agent if layout.placement == "visiting" else None,
        )
        if status is None:
            status = user_call(process.status, ui_base_url=ui_base_url())
            if status is None:
                raise ClickException(f"Agent {agent_name} not found")
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
    return _cap_counts_summary(counts)


def _local_resources(
    layout: AgentLayout,
    *,
    model_catalog: Path | None,
    source: Path | None = None,
    selector: str | None = None,
) -> list[tuple[str, str]]:
    progress = make_cli_progress()
    try:
        with progress:
            if source is not None:
                user_call(agents.materialize_roaming_program, source)
            elif selector is not None:
                user_call(
                    agents.resolve_visiting_layout, selector, progress=progress.sink
                )
            state = cast(
                AgentState,
                user_call(
                    prepare_agent_state,
                    layout,
                    progress=progress.sink,
                ),
            )
            watcher = (
                SetupWatcher(layout, model_catalog=model_catalog)
                if model_catalog is not None
                else SetupWatcher(layout)
            )
            setup = asyncio.run(watcher.refresh(progress=progress.sink))
            return [
                ("Tools", _tools_summary(setup, progress=progress.sink)),
                ("Models", _models_summary(setup, progress=progress.sink)),
                ("Caps", _caps_summary(state)),
                ("Jobs", _jobs_summary(layout)),
                ("Workspaces", ", ".join(setup.workspace_grants(state.workspaces))),
            ]
    except Exception as exc:
        if progress.failure_stage is not None:
            raise ClickException(progress.failure_message(exc)) from exc
        raise


def _running_resources(
    client: RuntimeClient, *, progress: ProgressSink | None = None
) -> list[tuple[str, str]]:
    """Inspect the executor's published resources, including its startup overrides."""
    resources = client.inspect_resources(progress=progress)
    models = _resource_items(resources["models"], "models")
    tools = _resource_items(resources["tools"], "tools")
    caps = resources["caps"]
    if not isinstance(caps, Mapping):
        raise ValueError("runtime returned invalid caps")
    cap_counts = {
        kind: len(_resource_list(caps.get(kind), kind))
        for kind in ("psyches", "skills", "services", "prompts")
    }
    chores = _resource_list(resources["chores"], "chores")
    tasks = _resource_list(resources["tasks"], "tasks")
    workspaces = WorkspaceInspection.model_validate(resources["workspaces"])
    return [
        (
            "Tools",
            f"{_count(len(tools), 'tool')}, {_count(len(_resource_groups(tools, 'toolset')), 'toolset')}",
        ),
        (
            "Models",
            f"{_count(len(models), 'model')}, {_count(len(_resource_groups(models, 'provider')), 'provider')}",
        ),
        ("Caps", _cap_counts_summary(cap_counts)),
        ("Jobs", f"{_count(len(chores), 'chore')}, {_count(len(tasks), 'task')}"),
        ("Workspaces", ", ".join(item.name for item in workspaces.items)),
    ]


def _resource_items(value: object, label: str) -> list[dict[str, object]]:
    if not isinstance(value, dict):
        raise ValueError(f"runtime returned invalid {label}")
    return _resource_list(cast(dict[str, object], value).get("items"), label)


def _resource_list(value: object, label: str) -> list[dict[str, object]]:
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise ValueError(f"runtime returned invalid {label}")
    return cast(list[dict[str, object]], value)


def _resource_groups(items: list[dict[str, object]], field: str) -> set[str]:
    values = [item.get(field) for item in items]
    if any(not isinstance(value, str) or not value for value in values):
        raise ValueError(f"runtime returned invalid {field}")
    return set(cast(list[str], values))


def _count(value: int, singular: str) -> str:
    return f"{value} {singular if value == 1 else singular + 's'}"


def _cap_counts_summary(counts: Mapping[str, int]) -> str:
    return ", ".join(
        _count(counts[kind + "s"], kind)
        for kind in ("psyche", "skill", "service", "prompt")
    )


def _jobs_summary(layout: AgentLayout) -> str:
    catalog = AuthoredJobs(layout.home)
    chore_count = len(catalog.list(kind="chore"))
    task_count = len(catalog.list(kind="task"))
    return (
        f"{chore_count} {'chore' if chore_count == 1 else 'chores'}, "
        f"{task_count} {'task' if task_count == 1 else 'tasks'}"
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
