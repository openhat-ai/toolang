"""Agent catalog commands."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
import os
from pathlib import Path
from typing import Annotated

import click
import typer

from toolang.catalog.job import JobCatalog
from toolang.catalog.agent import AgentCatalog
from toolang.agent import local as agents
from toolang.catalog import cap as caps
from toolang.state.prepared import PreparedEntry
from ...common.client import append_agent_update
from ...common.context import (
    context_root,
    require_runtime_agent,
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
        layout = AgentCatalog(root).create(agent, template=template)
    except FileExistsError as exc:
        raise click.ClickException(f"Agent {agent} already exists") from exc
    append_agent_update(
        root,
        agent,
        "created",
        {"path": str(layout.program)},
    )
    typer.echo(f"Created agent {agent}: {layout.program}")


def clone_agent(
    ctx: typer.Context,
    source: Annotated[str, typer.Argument(help="Agent source selector.")],
    target: Annotated[str | None, typer.Argument(help="New local agent name.")] = None,
) -> None:
    root = context_root(ctx)
    try:
        layout = AgentCatalog(root).clone(source, target)
    except FileExistsError as exc:
        target_name = target or Path(source).stem
        raise click.ClickException(f"Agent {target_name} already exists") from exc
    except FileNotFoundError as exc:
        raise click.ClickException(f"Agent {source} not found") from exc
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    append_agent_update(
        root,
        layout.name,
        "created",
        {"path": str(layout.program), "source": source},
    )
    typer.echo(f"Cloned agent {layout.name}: {layout.program}")


def remove_agent(
    ctx: typer.Context,
    agent: Annotated[str, typer.Argument(help="Agent name")],
) -> None:
    root = context_root(ctx)
    try:
        AgentCatalog(root).remove(agent)
    except FileNotFoundError as exc:
        raise click.ClickException(f"Agent {agent} not found") from exc
    except ValueError as exc:
        status = agents.AgentProcess(root, agent).status(ui_base_url=ui_base_url())
        if status is not None and status.status in {"running", "preparing", "starting"}:
            raise click.ClickException(active_agent_error(status)) from exc
        raise click.ClickException(f"Agent {agent} already running") from exc
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
) -> None:
    agent_name = require_runtime_agent(ctx, agent)
    root = context_root(ctx)
    process = agents.AgentProcess(root, agent_name)
    status = user_call(process.status, ui_base_url=ui_base_url())
    if status is None:
        raise click.ClickException(f"Agent {agent_name} not found")
    runtime_state = process.state() or {}
    created_at = created_time(agents.agent_home(root, agent_name))
    started_at = runtime_value(runtime_state.get("started_at"))
    updated_at = runtime_value(runtime_state.get("updated_at"))
    status_value = status.status
    if status.status == "running" and started_at != "-":
        online = _human_uptime_since(started_at)
        if online is not None:
            status_value = f"{status.status} ({online})"
    rows = [
        ("Home", str(agents.agent_home(root, agent_name))),
        ("Caps", _caps_summary(root, agent_name)),
        ("Jobs", _jobs_summary(root, agent_name)),
        ("Tools", _tools_summary(root, agent_name)),
        (
            "Models",
            _models_summary(
                root,
                agent_name,
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
    pid_text = agents.runtime_pid_label(runtime_state)
    if pid_text is not None and status.status != "stopped":
        rows.append(("PID", pid_text))
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


def _caps_summary(root: Path, agent: str) -> str:
    counts = _prepared_cap_counts(root, agent)
    if counts is None:
        counts = _prepare_cap_counts(root, agent)
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


def _prepared_cap_counts(root: Path, agent: str) -> dict[str, int] | None:
    from toolang.state.prepared import load_private_lock, load_shared_lock

    try:
        shared_lock = load_shared_lock(root)
        private_lock = load_private_lock(root, agent)
    except (FileNotFoundError, OSError, TypeError, ValueError, KeyError):
        return None
    return _cap_counts(caps.effective_cap_entries(shared_lock, private_lock))


def _cap_counts(entries: Sequence[PreparedEntry]) -> dict[str, int]:
    counts = {"psyches": 0, "skills": 0, "services": 0, "prompts": 0}
    for entry in entries:
        key = f"{entry.kind}s"
        if key in counts:
            counts[key] += 1
    return counts


def _prepare_cap_counts(root: Path, agent: str) -> dict[str, int]:
    from toolang.agent import runtime as up

    progress = make_cli_progress(show_materialize_summary=True)
    try:
        state = user_call(
            up.prepare_agent,
            toolang_root=root,
            agent_name=agent,
            progress=as_progress_sink(progress),
        )
        progress.set_prepare_total(len(state.caps))
        return _cap_counts(state.caps)
    finally:
        progress.finish(details=False)


def _jobs_summary(root: Path, agent: str) -> str:
    catalog = JobCatalog(root, agent)
    chore_count = len(catalog.list(kind="chore"))
    task_count = len(catalog.list(kind="task"))
    return (
        f"{chore_count} {'chore' if chore_count == 1 else 'chores'}, "
        f"{task_count} {'task' if task_count == 1 else 'tasks'}"
    )


def _models_summary(
    root: Path,
    agent: str,
    *,
    runtime_state: dict[str, object],
    running: bool,
) -> str:
    from toolang.agent import runtime as up

    selectors: Sequence[str] = ()
    raw_models = runtime_state.get("models")
    if running and isinstance(raw_models, list):
        selectors = tuple(
            value.strip()
            for item in raw_models
            if isinstance(item, str) and (value := item.strip())
        )
    if not selectors:
        selectors = up.load_default_models(root, agent)
    rows = plugin.model_rows(
        root,
        dict(os.environ),
        agent_name=agent,
        model_selectors=selectors,
    )
    provider_count = len({provider for _model, provider, _detail in rows})
    return (
        f"{len(rows)} {'model' if len(rows) == 1 else 'models'}, "
        f"{provider_count} {'provider' if provider_count == 1 else 'providers'}"
    )


def _tools_summary(root: Path, agent: str) -> str:
    rows = plugin.tool_rows(root, dict(os.environ), agent_name=agent)
    set_count = len({namespace for namespace, _tool, _description in rows})
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
