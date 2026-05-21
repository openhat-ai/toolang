"""Typer CLI for Toolang agent management."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version
from pathlib import Path
import os
import subprocess
import sys
import time
import tomllib
from typing import Annotated, Literal, cast

import click
import typer
from typer.core import TyperCommand

from .. import agents, caps as cap_store, templates, work
from .. import up as agent_up
from ..base.protocols.model import ModelProvider
from ..base.types.model import ModelInfo
from ..config.log import LoggingPlan, configure_logging, configure_logging_plan, resolve_agent_logging
from ..execution.model import DEFAULT_MODEL_SELECTOR
from ..execution.records import UpdateKind
from ..models.discovery import (
    default_provider_api_key_env,
    default_provider_base_url,
    missing_provider_env_vars,
    model_infos,
    required_provider_env_vars,
)
from . import caps as caps_cli
from . import invoke as cli_invoke
from .progress import CliProgress, as_progress_sink, make_cli_progress
from .utils import (
    _AGENT_AVATAR,
    _PrefixAgentWorkGroup,
    _RequiredPrefixAgentCommand,
    _RunAgentCommand,
    _RuntimeAgentCommand,
    _append_agent_update,
    _context_root,
    _created_time,
    _echo_pairs_table,
    _echo_table,
    _normalize_feature_option,
    _parse_utc_timestamp,
    _required_prefix_agent,
    _required_runtime_agent,
    _runtime_environ_for_agent,
    _runtime_features,
    _runtime_value,
    _toolang_root,
    _ui_base_url,
    _wait_for_started_status,
    _wrap_user_error,
)

WorkKind = Literal["task", "chore"]
_CLI_PREFIX_AGENT: str | None = None
AGENT_COMMAND_PANEL = "Agent Commands"
RUNTIME_COMMAND_PANEL = "Runtime Commands"
TOP_LEVEL_COMMANDS = frozenset(
    {
        "new",
        "clone",
        "remove",
        "list",
        "info",
        "model",
        "plugin",
        "run",
        "start",
        "stop",
        "task",
        "chore",
        "skill",
        "psyche",
        "service",
        "prompt",
    }
)


@dataclass(frozen=True, slots=True)
class _RuntimeStartup:
    """Resolved and prepared inputs for one runtime launch."""

    target: agents.MaterializedRunTarget
    startup: agent_up.StartupSpec
    environ: dict[str, str]
    log_plan: LoggingPlan


POSTFIX_AGENT_COMMANDS = frozenset({"run", "start", "stop", "info"})
PREFIX_AGENT_COMMANDS = frozenset({"run", "start", "stop", "task", "chore", "skill", "psyche", "service", "prompt"})


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _human_uptime_since(timestamp_text: str) -> str | None:
    started = _parse_utc_timestamp(timestamp_text)
    if started is None:
        return None
    delta = _utc_now() - started
    total_seconds = max(int(delta.total_seconds()), 0)
    import humanize

    return f"up {humanize.naturaldelta(total_seconds)}"


def _toolang_version() -> str:
    return f"{_base_toolang_version()}{_source_state_suffix()}"


def _base_toolang_version() -> str:
    try:
        return package_version("toolang")
    except PackageNotFoundError:
        pyproject_path = Path(__file__).resolve().parents[3] / "pyproject.toml"
        try:
            data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            return "unknown"
        project = data.get("project")
        if not isinstance(project, dict):
            return "unknown"
        version = project.get("version")
        return version if isinstance(version, str) else "unknown"


def _source_state_suffix() -> str:
    source_root = _source_tree_root()
    if source_root is None:
        return ""
    short_sha = _git_output(source_root, "rev-parse", "--short", "HEAD")
    if short_sha is None:
        return ""
    dirty = _git_output(source_root, "status", "--short")
    if dirty is None:
        return f"+{short_sha}"
    dirty_suffix = "*" if dirty else ""
    return f"+{short_sha}{dirty_suffix}"


def _git_output(source_root: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=source_root,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _source_tree_root() -> Path | None:
    current = Path(__file__).resolve()
    for parent in current.parents:
        if (parent / ".git").exists():
            return parent
    return None


def _version_callback(value: bool) -> None:
    if not value:
        return
    typer.echo(f"toolang {_toolang_version()}")
    raise typer.Exit()


app = typer.Typer(
    help="Run and manage Toolang agents.",
    add_completion=False,
    invoke_without_command=True,
    no_args_is_help=True,
    pretty_exceptions_enable=False,
    pretty_exceptions_show_locals=False,
)


@app.callback()
def callback(
    ctx: typer.Context,
    toolang_root: Annotated[
        Path | None,
        typer.Option("--root", "-r", help="Root directory for all agents."),
    ] = None,
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            "-V",
            callback=_version_callback,
            help="Show current version and exit.",
            is_eager=True,
        ),
    ] = False,
) -> None:
    """Toolang CLI."""

    del version
    try:
        configure_logging(spec=None, environ=os.environ)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    ctx.obj = {
        "toolang_root": _toolang_root(toolang_root),
        "agent": _CLI_PREFIX_AGENT,
    }


@app.command("new", help="Create an agent.", no_args_is_help=True, rich_help_panel=AGENT_COMMAND_PANEL)
def new_agent(
    ctx: typer.Context,
    agent: Annotated[str, typer.Argument(help="Agent name")],
    template: Annotated[
        str,
        typer.Option("--template", "-t", help="Template name."),
    ] = "default",
) -> None:
    try:
        program_path = agents.create_agent(
            _context_root(ctx),
            agent,
            template_name=template,
        )
    except FileExistsError as exc:
        raise click.ClickException(f"Agent {agent} already exists") from exc
    _append_agent_update(
        _context_root(ctx),
        agent,
        "created",
        {"path": str(program_path)},
    )
    typer.echo(f"Created agent {agent}: {program_path}")


@app.command("clone", help="Clone an agent.", no_args_is_help=True, rich_help_panel=AGENT_COMMAND_PANEL)
def clone_agent(
    ctx: typer.Context,
    source: Annotated[str, typer.Argument(help="Agent source selector.")],
    target: Annotated[str | None, typer.Argument(help="New local agent name.")] = None,
) -> None:
    try:
        program_path = agents.clone_agent(_context_root(ctx), source, target)
    except FileExistsError as exc:
        target_name = target or Path(source).stem
        raise click.ClickException(f"Agent {target_name} already exists") from exc
    except FileNotFoundError as exc:
        raise click.ClickException(f"Agent {source} not found") from exc
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    target_name = program_path.parent.name
    _append_agent_update(
        _context_root(ctx),
        target_name,
        "created",
        {"path": str(program_path), "source": source},
    )
    typer.echo(f"Cloned agent {target_name}: {program_path}")


@app.command("remove", help="Remove an agent.", no_args_is_help=True, rich_help_panel=AGENT_COMMAND_PANEL)
def remove_agent(
    ctx: typer.Context,
    agent: Annotated[str, typer.Argument(help="Agent name")],
) -> None:
    root = _context_root(ctx)
    try:
        agents.remove_agent(root, agent)
    except FileNotFoundError as exc:
        raise click.ClickException(f"Agent {agent} not found") from exc
    except ValueError as exc:
        status = agents.get_agent_status(root, agent, ui_base_url=_ui_base_url())
        if status is not None and status.status in {"running", "preparing", "starting"}:
            raise click.ClickException(_active_run_error(status)) from exc
        raise click.ClickException(f"Agent {agent} already running") from exc
    typer.echo(f"Removed agent {agent}")


@app.command("list", help="Show agents and their status.", rich_help_panel=AGENT_COMMAND_PANEL)
def list_agents(ctx: typer.Context) -> None:
    items = agents.list_agent_statuses(
        _context_root(ctx),
        ui_base_url=_ui_base_url(),
    )
    if not items:
        typer.echo("No agents found.")
        return
    rows = [
        (
            item.name,
            item.status,
            item.sandbox if item.status == "running" and item.sandbox else "-",
            item.api_url or "-",
            item.webui_url or "-",
        )
        for item in items
    ]
    _echo_table(("AGENT", "STATUS", "SANDBOX", "API", "WEBUI"), rows)


@app.command(
    "info",
    help="Show agent info.",
    no_args_is_help=True,
    cls=_RuntimeAgentCommand,
    rich_help_panel=AGENT_COMMAND_PANEL,
)
def info_agent(
    ctx: typer.Context,
    agent: str | None = typer.Argument(None, help="Agent name", hidden=True),
) -> None:
    agent_name = _required_runtime_agent(ctx, agent)
    root = _context_root(ctx)
    status = _wrap_user_error(
        agents.get_agent_status,
        root,
        agent_name,
        ui_base_url=_ui_base_url(),
    )
    if status is None:
        raise click.ClickException(f"Agent {agent_name} not found")
    runtime_state = agents.load_runtime_state(root, agent_name) or {}
    created_at = _created_time(agents.agent_home(root, agent_name))
    started_at = _runtime_value(runtime_state.get("started_at"))
    updated_at = _runtime_value(runtime_state.get("updated_at"))
    status_value = status.status
    if status.status == "running" and started_at != "-":
        online = _human_uptime_since(started_at)
        if online is not None:
            status_value = f"{status.status} ({online})"
    rows = [
        ("Home", str(agents.agent_home(root, agent_name))),
        ("Caps", _info_caps_summary(root, agent_name)),
        ("Jobs", _info_jobs_summary(root, agent_name)),
        ("Models", _info_models_summary(root, agent_name, runtime_state=runtime_state, running=status.status != "stopped")),
        ("Status", status_value),
    ]
    if status.status == "stopped":
        rows.append(("Created", created_at))
        _echo_pairs_table(rows, avatar=_AGENT_AVATAR, title=agent_name.upper())
        return
    if status.sandbox:
        rows.append(("Sandbox", status.sandbox))
    if status.status == "running":
        loops_text = _runtime_features(runtime_state)
        if loops_text is not None:
            rows.append(("Features", loops_text))
    message = _runtime_value(runtime_state.get("message"))
    pid_text = agents.runtime_pid_label(runtime_state)
    if pid_text is not None and status.status != "stopped":
        rows.append(("PID", pid_text))
    if status.api_url:
        rows.append(("API", status.api_url))
    if status.webui_url:
        rows.append(("WebUI", status.webui_url))
    if status.status == "running" and started_at != "-":
        rows.append(("Started", started_at))
        rows.append(("Created", created_at))
    if status.status != "running" and updated_at != "-":
        rows.append(("Updated", updated_at))
    if status.status != "running" and message != "-":
        rows.append(("Message", message))
    _echo_pairs_table(rows, avatar=_AGENT_AVATAR, title=agent_name.upper())


@app.command(
    "run",
    help="Run an agent in the foreground.",
    no_args_is_help=True,
    cls=_RunAgentCommand,
    rich_help_panel=AGENT_COMMAND_PANEL,
)
def run_agent(
    ctx: typer.Context,
    agent: str | None = typer.Argument(None, help="Agent selector", hidden=True),
    sandbox: Annotated[
        str | None,
        typer.Option(help="Sandbox to use: none or <driver>[:target]."),
    ] = None,
    models: Annotated[
        list[str] | None,
        typer.Option(
            "--model",
            help="Allow a model selector for this activation. Repeat to allow multiple; the first becomes default.",
        ),
    ] = None,
    features: Annotated[
        list[str] | None,
        typer.Option("--feature", help="Runtime feature to enable. Repeat or pass CSV."),
    ] = None,
    port: Annotated[int | None, typer.Option(help="Port to listen on.")] = None,
    host: Annotated[str, typer.Option(help="Host interface to bind.")] = "127.0.0.1",
    dev: Annotated[
        Path | None,
        typer.Option(
            "--dev",
            help="Wheel file, or a directory tree containing wheels, for managed sandbox startup.",
        ),
    ] = None,
    endpoint_host: Annotated[
        str | None,
        typer.Option("--endpoint-host", help="Endpoint host name.", hidden=True),
    ] = None,
    sandbox_child: Annotated[
        bool,
        typer.Option("--sandbox-child", hidden=True),
    ] = False,
) -> None:
    selector = _required_runtime_agent(ctx, agent)
    normalized_features = _normalize_feature_option(features)
    root = _context_root(ctx)
    progress = make_cli_progress()
    progress_finished = False
    try:
        with agents.resolved_run_target(root, selector, progress=as_progress_sink(progress)) as target:
            launch = _resolve_runtime_startup(
                ctx,
                target,
                sandbox=sandbox,
                models=models,
                features=normalized_features,
                port=port,
                host=host,
                endpoint_host=endpoint_host,
                dev=dev,
                background=False,
                progress=progress,
            )
            progress.finish(details=False)
            progress_finished = True
            raise typer.Exit(
                _wrap_user_error(
                    agent_up.start_runtime,
                    launch.startup,
                    environ=launch.environ,
                    sandbox_child=sandbox_child,
                    progress=None,
                )
            )
    except KeyboardInterrupt:
        if not progress_finished:
            progress.interrupt()
        raise typer.Exit(130) from None
    except (FileExistsError, FileNotFoundError, ValueError, click.ClickException) as exc:
        if not progress_finished:
            progress.finish(details=False)
        if isinstance(exc, click.ClickException):
            raise
        raise click.ClickException(str(exc)) from exc


def _active_run_error(status: agents.AgentStatus) -> str:
    message = f"Agent {status.name} already {status.status}"
    detail = (status.webui_url or status.api_url) if status.status == "running" else None
    if detail:
        return f"{message}: {detail}"
    return message


def _resolve_runtime_startup(
    ctx: typer.Context,
    target: agents.MaterializedRunTarget,
    *,
    sandbox: str | None,
    models: list[str] | None,
    features: list[str] | None,
    port: int | None,
    host: str,
    endpoint_host: str | None,
    dev: Path | None,
    background: bool,
    progress: CliProgress | None,
) -> _RuntimeStartup:
    run_root = target.toolang_root
    agent_name = target.agent_name
    if target.kind == "resident" and not agents.agent_home(run_root, agent_name).is_dir():
        raise click.ClickException(f"Agent {agent_name} not found")
    existing = agents.get_agent_status(run_root, agent_name, ui_base_url=_ui_base_url())
    if existing is not None and existing.status in {"running", "preparing", "starting"}:
        raise click.ClickException(_active_run_error(existing))
    environ = _runtime_environ_for_agent(ctx, agent_name, toolang_root=run_root)
    environ["TOOLANG_ROOT"] = str(run_root)
    log_plan = resolve_agent_logging(
        mode="start" if background else "run",
        environ=environ,
        agent_log_path=agents.agent_runtime_log_path(run_root, agent_name),
    )
    if not background:
        configure_logging_plan(log_plan)
    startup = _wrap_user_error(
        agent_up.resolve_startup,
        toolang_root=run_root,
        agent_name=agent_name,
        host=host,
        endpoint_host=endpoint_host,
        port=port,
        sandbox=sandbox,
        models=models,
        dev=dev,
        feature_names=features,
        log_spec=log_plan.spec,
        temporary_port=target.kind == "visiting" and port is None,
        environ=log_plan.environ,
    )
    _wrap_user_error(
        agent_up.prepare_agent,
        toolang_root=run_root,
        agent_name=agent_name,
        progress=as_progress_sink(progress),
    )
    return _RuntimeStartup(target=target, startup=startup, environ=log_plan.environ, log_plan=log_plan)


@app.command(
    "start",
    help="Start an agent.",
    no_args_is_help=True,
    cls=_RuntimeAgentCommand,
    rich_help_panel=AGENT_COMMAND_PANEL,
)
def start_agent(
    ctx: typer.Context,
    agent: str | None = typer.Argument(None, help="Agent name", hidden=True),
    sandbox: Annotated[
        str | None,
        typer.Option(help="Sandbox to use: none or <driver>[:target]."),
    ] = None,
    models: Annotated[
        list[str] | None,
        typer.Option(
            "--model",
            help="Allow a model selector for this activation. Repeat to allow multiple; the first becomes default.",
        ),
    ] = None,
    features: Annotated[
        list[str] | None,
        typer.Option("--feature", help="Runtime feature to enable. Repeat or pass CSV."),
    ] = None,
    port: Annotated[int | None, typer.Option(help="Port to listen on.")] = None,
    host: Annotated[str, typer.Option(help="Host interface to bind.")] = "127.0.0.1",
    dev: Annotated[
        Path | None,
        typer.Option(
            "--dev",
            help="Wheel file, or a directory tree containing wheels, for managed sandbox startup.",
        ),
    ] = None,
    endpoint_host: Annotated[
        str | None,
        typer.Option("--endpoint-host", help="Endpoint host name.", hidden=True),
    ] = None,
) -> None:
    selector = _required_runtime_agent(ctx, agent)
    parsed_selector = _wrap_user_error(agents.parse_agent_selector, selector)
    if parsed_selector.form != "name":
        raise click.ClickException("start only supports local agent names; clone the remote source first")
    root = _context_root(ctx)
    normalized_features = _normalize_feature_option(features)
    progress = make_cli_progress()
    try:
        with agents.resolved_run_target(root, selector, progress=as_progress_sink(progress)) as target:
            launch = _resolve_runtime_startup(
                ctx,
                target,
                sandbox=sandbox,
                models=models,
                features=normalized_features,
                port=port,
                host=host,
                endpoint_host=endpoint_host,
                dev=dev,
                background=True,
                progress=progress,
            )
    except KeyboardInterrupt:
        progress.interrupt()
        raise typer.Exit(130) from None
    except click.ClickException:
        progress.finish(details=False)
        raise
    agent_name = launch.target.agent_name
    root = launch.target.toolang_root
    progress.finish(details=False)
    if launch.log_plan.path is None:
        raise click.ClickException("agent log path was not resolved")
    log_path = launch.log_plan.path
    log_path.parent.mkdir(parents=True, exist_ok=True)
    launched_at = time.time()
    command = [
        sys.executable,
        "-m",
        "toolang.cli.main",
        *agent_up.build_run_argv(launch.startup),
    ]
    with log_path.open("ab") as stream:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=stream,
            stderr=stream,
            env=launch.environ,
            cwd=str(Path.cwd()),
            start_new_session=True,
            close_fds=True,
        )
    status = _wait_for_started_status(
        root=root,
        agent_name=agent_name,
        process=process,
        launched_at=launched_at,
        timeout_sec=30.0,
    )
    if status is None:
        if process.poll() is not None:
            raise click.ClickException(f"Agent {agent_name} failed to start: {log_path}")
        raise click.ClickException(f"Agent {agent_name} start timed out: {log_path}")
    if status.status == "failed":
        raise click.ClickException(f"Agent {agent_name} failed to start: {log_path}")
    typer.echo(f"Started agent {agent_name}: {status.webui_url or status.api_url or status.endpoint or '-'}")


@app.command(
    "stop",
    help="Stop an agent.",
    no_args_is_help=True,
    cls=_RuntimeAgentCommand,
    rich_help_panel=AGENT_COMMAND_PANEL,
)
def stop_agent(
    ctx: typer.Context,
    agent: str | None = typer.Argument(None, help="Agent name", hidden=True),
    force: Annotated[
        bool,
        typer.Option(help="Force-stop when graceful shutdown does not complete."),
    ] = False,
) -> None:
    agent_name = _required_runtime_agent(ctx, agent)
    root = _context_root(ctx)
    runtime_state = agents.load_runtime_state(root, agent_name)
    runtime_pids = () if runtime_state is not None else agents.agent_runtime_process_pids(root, agent_name)
    if runtime_state is None and not runtime_pids:
        raise click.ClickException(f"Agent {agent_name} not running")

    sandbox_plugin = None
    sandbox = runtime_state.get("sandbox") if runtime_state is not None else None
    if isinstance(sandbox, dict):
        sandbox_data = {str(key): value for key, value in sandbox.items()}
        selector = sandbox_data.get("selector")
        if not isinstance(selector, dict):
            raise click.ClickException(f"Sandbox state is invalid for agent: {agent}")
        selector_data = {str(key): value for key, value in selector.items()}
        driver = selector_data.get("driver")
        if not isinstance(driver, str) or not driver.strip():
            raise click.ClickException(f"Sandbox driver is missing for agent: {agent_name}")
        sandbox_plugin = agent_up.create_sandbox_plugin(driver.strip(), config={})

    stopped = _wrap_user_error(
        agents.stop_agent,
        root,
        agent_name,
        sandbox_plugin=sandbox_plugin,
        force=force,
    )
    typer.echo(f"Stopped agent {agent_name}" if stopped else f"Agent {agent_name} not running")


def _info_caps_summary(toolang_root: Path, agent_name: str) -> str:
    counts = {
        "psyches": len(cap_store.list_entries(toolang_root, agent_name, visibility=None, kinds={"psyche"})),
        "skills": len(cap_store.list_entries(toolang_root, agent_name, visibility=None, kinds={"skill"})),
        "services": len(cap_store.list_entries(toolang_root, agent_name, visibility=None, kinds={"service"})),
        "prompts": len(cap_store.list_entries(toolang_root, agent_name, visibility=None, kinds={"prompt"})),
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


def _info_jobs_summary(toolang_root: Path, agent_name: str) -> str:
    chore_count = len(work.list_chores(toolang_root, agent_name))
    task_count = len(work.list_tasks(toolang_root, agent_name))
    return (
        f"{chore_count} {'chore' if chore_count == 1 else 'chores'}, "
        f"{task_count} {'task' if task_count == 1 else 'tasks'}"
    )


def _info_models_summary(
    toolang_root: Path,
    agent_name: str,
    *,
    runtime_state: dict[str, object],
    running: bool,
) -> str:
    if running:
        raw_models = runtime_state.get("models")
        if isinstance(raw_models, list):
            runtime_models = tuple(
                str(item).strip()
                for item in raw_models
                if isinstance(item, str) and item.strip()
            )
            if runtime_models:
                return ", ".join(runtime_models)
    configured = agent_up.load_default_models(toolang_root, agent_name)
    if configured:
        return ", ".join(configured)
    return DEFAULT_MODEL_SELECTOR


plugin_app = typer.Typer(
    help="Inspect installed plugins.",
    add_completion=False,
    no_args_is_help=True,
    pretty_exceptions_enable=False,
    pretty_exceptions_show_locals=False,
)


model_app = typer.Typer(
    help="Inspect discoverable models.",
    add_completion=False,
    no_args_is_help=True,
    pretty_exceptions_enable=False,
    pretty_exceptions_show_locals=False,
)


@model_app.command("list", help="List discoverable models.")
def list_models() -> None:
    environ = dict(os.environ)
    rows = _model_rows(environ)
    if not rows:
        typer.echo("No discoverable models found.")
        return
    _echo_table(("PROVIDER", "MODEL", "ADAPTER", "FEATURES"), rows)


@plugin_app.command("list", help="List installed plugins.")
def list_plugins() -> None:
    environ = dict(os.environ)
    rows = _plugin_rows(environ)
    if not rows:
        typer.echo("No plugins found.")
        return
    _echo_table(("FAMILY", "NAME", "SOURCE", "CONFIG", "DETAILS"), rows)


def _model_rows(environ: dict[str, str]) -> list[tuple[str, str, str, str]]:
    rows: list[tuple[str, str, str, str]] = []
    for name, provider in sorted(agent_up.load_model_providers().items()):
        for info in model_infos(provider, environ=environ):
            rows.append(
                (
                    name,
                    info.ref,
                    info.adapter,
                    _model_feature_summary(info),
                )
            )
    return rows


def _model_feature_summary(info: ModelInfo) -> str:
    parts: list[str] = []
    parts.append(f"tools={'yes' if info.tools else 'no'}")
    parts.append(f"streaming={'yes' if info.streaming else 'no'}")
    if info.context_window is not None:
        parts.append(f"ctx={_format_k(info.context_window)}")
    if info.max_output_tokens is not None:
        parts.append(f"max_out={_format_k(info.max_output_tokens)}")
    if info.input_price is not None or info.output_price is not None:
        in_price = "-" if info.input_price is None else _format_price_per_million(info.input_price)
        out_price = "-" if info.output_price is None else _format_price_per_million(info.output_price)
        parts.append(f"price={in_price}/{out_price}")
    return ", ".join(parts)


def _format_k(value: int) -> str:
    if value >= 1024:
        return f"{value / 1024:g}k"
    return str(value)


def _format_price_per_million(value: float) -> str:
    return f"${value * 1_000_000:g}"


def _plugin_rows(environ: dict[str, str]) -> list[tuple[str, str, str, str, str]]:
    rows: list[tuple[str, str, str, str, str]] = []
    model_sources = _plugin_source_by_name("toolang.model")
    for name, provider in sorted(agent_up.load_model_providers().items()):
        rows.append(
            (
                "model",
                name,
                model_sources.get(name, _model_provider_source(provider)),
                _model_provider_config(provider, environ=environ),
                _model_provider_details(provider, environ=environ),
            )
        )
    for family, group in (
        ("tool", "toolang.tool"),
        ("channel", "toolang.channel"),
        ("sandbox", "toolang.sandbox"),
    ):
        rows.extend(
            (
                family,
                info.name,
                info.source,
                "available",
                "Entry point is discoverable.",
            )
            for info in agent_up.list_plugin_infos(group=group)
        )
    return rows


def _plugin_source_by_name(group: str) -> dict[str, str]:
    return {info.name: info.source for info in agent_up.list_plugin_infos(group=group)}


def _model_provider_source(provider: ModelProvider) -> str:
    return "built-in" if provider.__class__.__module__.startswith("toolang.") else "external"


def _model_provider_config(
    provider: ModelProvider,
    *,
    environ: dict[str, str],
) -> str:
    missing = missing_provider_env_vars(provider, environ=environ)
    return "missing env" if missing else "configured"


def _model_provider_details(
    provider: ModelProvider,
    *,
    environ: dict[str, str],
) -> str:
    missing = missing_provider_env_vars(provider, environ=environ)
    required = required_provider_env_vars(provider)
    base_url = default_provider_base_url(provider, environ=environ)
    api_key_env = default_provider_api_key_env(provider)
    models = model_infos(provider, environ=environ)
    parts: list[str] = []
    if base_url is not None:
        parts.append(f"base URL {base_url}")
    if required:
        parts.append(f"env {', '.join(required)}")
    elif api_key_env is not None:
        parts.append(f"default auth {api_key_env}")
    if missing:
        parts.append(f"missing {', '.join(missing)}")
    parts.append(f"{len(models)} discovered {'model' if len(models) == 1 else 'models'}")
    if provider.description:
        parts.append(provider.description)
    return "; ".join(parts)

def _append_work_update(
    toolang_root: Path,
    agent_name: str,
    *,
    kind: WorkKind,
    job_id: str,
    path: Path,
) -> None:
    update_kind = cast(UpdateKind, f"{kind}_changed")
    _append_agent_update(
        toolang_root,
        agent_name,
        update_kind,
        {
            "id": job_id,
            "path": str(path),
        },
    )


def register_work_commands() -> None:
    work_titles: dict[WorkKind, str] = {
        "chore": "Chore",
        "task": "Task",
    }
    work_group_help: dict[WorkKind, str] = {
        "chore": "Manage chores.",
        "task": "Manage tasks.",
    }

    @dataclass(frozen=True, slots=True)
    class WorkCommandSpec:
        name: str
        help: Callable[[WorkKind], str]
        factory: Callable[[WorkKind, str], Callable[..., None]]
        cls: type[TyperCommand] | None = None
        no_args_is_help: bool = False

    command_specs: tuple[WorkCommandSpec, ...] = (
        WorkCommandSpec(
            name="list",
            help=lambda kind: f"List {kind}s.",
            factory=_make_work_list_command,
            cls=_RequiredPrefixAgentCommand,
        ),
        WorkCommandSpec(
            name="new",
            help=lambda kind: f"Create a {kind}.",
            factory=_make_new_work_command,
            cls=_RequiredPrefixAgentCommand,
        ),
        WorkCommandSpec(
            name="edit",
            help=lambda kind: f"Edit a {kind}.",
            factory=_make_edit_work_command,
            cls=_RequiredPrefixAgentCommand,
            no_args_is_help=True,
        ),
        WorkCommandSpec(
            name="pause",
            help=lambda kind: f"Pause a {kind}.",
            factory=_make_pause_work_command,
            cls=_RequiredPrefixAgentCommand,
            no_args_is_help=True,
        ),
        WorkCommandSpec(
            name="resume",
            help=lambda kind: f"Resume a {kind}.",
            factory=_make_resume_work_command,
            cls=_RequiredPrefixAgentCommand,
            no_args_is_help=True,
        ),
        WorkCommandSpec(
            name="archive",
            help=lambda kind: f"Archive a {kind}.",
            factory=_make_archive_work_command,
            cls=_RequiredPrefixAgentCommand,
            no_args_is_help=True,
        ),
        WorkCommandSpec(
            name="restore",
            help=lambda kind: f"Restore an archived {kind}.",
            factory=_make_restore_work_command,
            cls=_RequiredPrefixAgentCommand,
            no_args_is_help=True,
        ),
        WorkCommandSpec(
            name="delete",
            help=lambda kind: f"Delete an archived {kind}.",
            factory=_make_delete_work_command,
            cls=_RequiredPrefixAgentCommand,
            no_args_is_help=True,
        ),
    )

    for kind in work_titles:
        title = work_titles[kind]
        work_app = typer.Typer(
            cls=_PrefixAgentWorkGroup,
            help=work_group_help[kind],
            add_completion=False,
            no_args_is_help=True,
            pretty_exceptions_enable=False,
            pretty_exceptions_show_locals=False,
        )
        for spec in command_specs:
            work_app.command(
                spec.name,
                help=spec.help(kind),
                cls=spec.cls,
                no_args_is_help=spec.no_args_is_help,
            )(spec.factory(kind, title))
        app.add_typer(work_app, name=kind, no_args_is_help=True, rich_help_panel=AGENT_COMMAND_PANEL)


def _make_work_list_command(kind: WorkKind, title: str) -> Callable[..., None]:
    def list_work(
        ctx: typer.Context,
        all_items: Annotated[
            bool,
            typer.Option("--all", help="Include archived items."),
        ] = False,
    ) -> None:
        agent_name = _required_prefix_agent(ctx, command_name=kind)
        root = _context_root(ctx)
        if kind == "task":
            entries = work.list_tasks(root, agent_name, include_archived=all_items)
            if not entries:
                typer.echo("No tasks found.")
                return
            rows = [
                (
                    entry.document.task_id(),
                    entry.document.display_title(fallback_name=entry.document.task_id()),
                    entry.document.state,
                    entry.document.stage,
                    _work_location(root, agent_name, entry.path),
                )
                for entry in entries
            ]
            _echo_table(("ID", title.upper(), "STATE", "STAGE", "LOCATION"), rows)
            return
        entries = work.list_chores(root, agent_name, include_archived=all_items)
        if not entries:
            typer.echo("No chores found.")
            return
        rows = [
            (
                entry.document.chore_id(),
                entry.document.display_title(fallback_name=entry.document.chore_id()),
                entry.document.state,
                entry.document.schedule,
                _work_location(root, agent_name, entry.path),
            )
            for entry in entries
        ]
        _echo_table(("ID", title.upper(), "STATE", "SCHEDULE", "LOCATION"), rows)

    return list_work


def _make_new_work_command(kind: WorkKind, title: str) -> Callable[..., None]:
    def new_work(
        ctx: typer.Context,
    ) -> None:
        agent_name = _required_prefix_agent(ctx, command_name=kind)
        text = click.edit(
            templates.render_template(kind, "default", agent_name=agent_name),
            extension=".md",
            require_save=True,
        )
        if text is None:
            raise typer.Exit()
        path = _wrap_user_error(
            work.create_task_text if kind == "task" else work.create_chore_text,
            _context_root(ctx),
            agent_name,
            text,
        )
        job_id = path.stem
        _append_work_update(_context_root(ctx), agent_name, kind=kind, job_id=job_id, path=path)
        typer.echo(f"{kind} {job_id} created\t{path}")

    return new_work


def _make_edit_work_command(kind: WorkKind, title: str) -> Callable[..., None]:
    def edit_work(
        ctx: typer.Context,
        id: str = typer.Argument(..., help=f"{title} id", metavar="ID"),
    ) -> None:
        job_id = id
        agent_name = _required_prefix_agent(ctx, command_name=kind)
        text = _wrap_user_error(
            work.load_task_text if kind == "task" else work.load_chore_text,
            _context_root(ctx),
            agent_name,
            job_id,
        )
        updated_text = click.edit(
            text,
            extension=".md",
            require_save=True,
        )
        if updated_text is None:
            raise typer.Exit()
        path = _wrap_user_error(
            work.update_task_text if kind == "task" else work.update_chore_text,
            _context_root(ctx),
            agent_name,
            job_id,
            updated_text,
        )
        _append_work_update(_context_root(ctx), agent_name, kind=kind, job_id=job_id, path=path)
        typer.echo(str(path))

    return edit_work


def _make_pause_work_command(kind: WorkKind, title: str) -> Callable[..., None]:
    def pause_work(
        ctx: typer.Context,
        id: str = typer.Argument(..., help=f"{title} id", metavar="ID"),
    ) -> None:
        job_id = id
        agent_name = _required_prefix_agent(ctx, command_name=kind)
        path = _wrap_user_error(
            work.pause_task if kind == "task" else work.pause_chore,
            _context_root(ctx),
            agent_name,
            job_id,
        )
        if path is None:
            raise click.ClickException(f"{kind} not found: {job_id}")
        _append_work_update(_context_root(ctx), agent_name, kind=kind, job_id=job_id, path=path)
        typer.echo(f"{kind} {job_id} paused\t{path}")

    return pause_work


def _make_resume_work_command(kind: WorkKind, title: str) -> Callable[..., None]:
    def resume_work(
        ctx: typer.Context,
        id: str = typer.Argument(..., help=f"{title} id", metavar="ID"),
    ) -> None:
        job_id = id
        agent_name = _required_prefix_agent(ctx, command_name=kind)
        path = _wrap_user_error(
            work.resume_task if kind == "task" else work.resume_chore,
            _context_root(ctx),
            agent_name,
            job_id,
        )
        if path is None:
            raise click.ClickException(f"{kind} not found: {job_id}")
        _append_work_update(_context_root(ctx), agent_name, kind=kind, job_id=job_id, path=path)
        typer.echo(f"{kind} {job_id} resumed\t{path}")

    return resume_work


def _make_archive_work_command(kind: WorkKind, title: str) -> Callable[..., None]:
    def archive_work(
        ctx: typer.Context,
        id: str = typer.Argument(..., help=f"{title} id", metavar="ID"),
    ) -> None:
        job_id = id
        agent_name = _required_prefix_agent(ctx, command_name=kind)
        path = _wrap_user_error(
            work.archive_task if kind == "task" else work.archive_chore,
            _context_root(ctx),
            agent_name,
            job_id,
        )
        if path is None:
            raise click.ClickException(f"{kind} not found: {job_id}")
        _append_work_update(_context_root(ctx), agent_name, kind=kind, job_id=job_id, path=path)
        typer.echo(f"{kind} {job_id} archived\t{path}")

    return archive_work


def _make_restore_work_command(kind: WorkKind, title: str) -> Callable[..., None]:
    def restore_work(
        ctx: typer.Context,
        id: str = typer.Argument(..., help=f"{title} id", metavar="ID"),
        inactive: Annotated[
            bool,
            typer.Option("--inactive", help="Restore as inactive instead of active."),
        ] = False,
    ) -> None:
        job_id = id
        agent_name = _required_prefix_agent(ctx, command_name=kind)
        state: Literal["active", "inactive"] = "inactive" if inactive else "active"
        path = _wrap_user_error(
            work.unarchive_task if kind == "task" else work.unarchive_chore,
            _context_root(ctx),
            agent_name,
            job_id,
            state=state,
        )
        if path is None:
            raise click.ClickException(f"archived {kind} not found: {job_id}")
        _append_work_update(_context_root(ctx), agent_name, kind=kind, job_id=job_id, path=path)
        typer.echo(f"{kind} {job_id} restored\t{path}")

    return restore_work


def _make_delete_work_command(kind: WorkKind, title: str) -> Callable[..., None]:
    def delete_work(
        ctx: typer.Context,
        id: str = typer.Argument(..., help=f"{title} id", metavar="ID"),
    ) -> None:
        job_id = id
        agent_name = _required_prefix_agent(ctx, command_name=kind)
        active_entry = (
            work.find_task(_context_root(ctx), agent_name, job_id)
            if kind == "task"
            else work.find_chore(_context_root(ctx), agent_name, job_id)
        )
        if active_entry is not None:
            raise click.ClickException(f"{kind} is not archived: {job_id}; archive it before deleting")
        entry = (
            work.find_archived_task(_context_root(ctx), agent_name, job_id)
            if kind == "task"
            else work.find_archived_chore(_context_root(ctx), agent_name, job_id)
        )
        if entry is None:
            raise click.ClickException(f"archived {kind} not found: {job_id}")
        removed = _wrap_user_error(
            work.remove_archived_task if kind == "task" else work.remove_archived_chore,
            _context_root(ctx),
            agent_name,
            job_id,
        )
        if not removed:
            raise click.ClickException(f"archived {kind} not found: {job_id}")
        path = entry.path
        _append_work_update(_context_root(ctx), agent_name, kind=kind, job_id=job_id, path=path)
        typer.echo(f"{kind} {job_id} deleted")

    return delete_work


def _work_location(toolang_root: Path, agent_name: str, path: Path) -> str:
    try:
        return str(path.relative_to(agents.agent_home(toolang_root, agent_name)))
    except ValueError:
        return str(path)

caps_cli.register_cap_commands(app, rich_help_panel=AGENT_COMMAND_PANEL)
app.add_typer(plugin_app, name="plugin", no_args_is_help=True, rich_help_panel=RUNTIME_COMMAND_PANEL)
app.add_typer(model_app, name="model", no_args_is_help=True, rich_help_panel=RUNTIME_COMMAND_PANEL)
register_work_commands()


def main(argv: Sequence[str] | None = None) -> int:
    global _CLI_PREFIX_AGENT
    raw_args = list(argv) if argv is not None else sys.argv[1:]
    global_args, body = _extract_global_args(raw_args)
    if body:
        roaming_source = cli_invoke.roaming_source_path(body[0])
        if roaming_source is not None:
            try:
                configure_logging(spec=None, environ={})
            except ValueError as exc:
                typer.echo(f"toolang error: {exc}", err=True)
                return 1
            return cli_invoke.handle_roaming_invoke(
                global_args,
                body,
                prog_name=_prog_name(sys.argv[0] if sys.argv else ""),
            )
    args, prefix_agent = _normalize_cli_args(raw_args)
    previous_prefix_agent = _CLI_PREFIX_AGENT
    _CLI_PREFIX_AGENT = prefix_agent
    try:
        app(
            args=args,
            prog_name=_prog_name(sys.argv[0] if sys.argv else ""),
            standalone_mode=True,
        )
    except click.exceptions.Exit as exc:
        return exc.exit_code
    except (FileExistsError, FileNotFoundError, ValueError) as exc:
        typer.echo(f"toolang error: {exc}", err=True)
        return 1
    finally:
        _CLI_PREFIX_AGENT = previous_prefix_agent
    return 0


def _prog_name(argv0: str) -> str:
    text = Path(argv0).name.strip()
    return text or "toolang"


def _normalize_cli_args(argv: list[str]) -> tuple[list[str], str | None]:
    global_args, body = _extract_global_args(argv)
    rewritten_body, prefix_agent = _rewrite_agent_shortcuts(body)
    return [*global_args, *rewritten_body], prefix_agent


def _extract_global_args(argv: list[str]) -> tuple[list[str], list[str]]:
    global_args: list[str] = []
    body: list[str] = []
    index = 0
    while index < len(argv):
        token = argv[index]
        consumed = _consume_global_arg(token, argv, index)
        if consumed is None:
            body.append(token)
            index += 1
            continue
        extracted, step = consumed
        global_args.extend(extracted)
        index += step
    return global_args, body


def _consume_global_arg(token: str, argv: list[str], index: int) -> tuple[list[str], int] | None:
    if token == "--root":
        if index + 1 >= len(argv):
            return ([token], 1)
        return ([token, argv[index + 1]], 2)
    if token.startswith("--root="):
        return (["--root", token.removeprefix("--root=")], 1)
    return None


def _rewrite_agent_shortcuts(body: list[str]) -> tuple[list[str], str | None]:
    if not body:
        return body, None
    if (
        len(body) >= 2
        and _looks_like_agent_name(body[0])
        and body[1] in POSTFIX_AGENT_COMMANDS
    ):
        return [body[1], body[0], *body[2:]], None
    if (
        len(body) >= 2
        and _looks_like_agent_name(body[0])
        and body[1] in PREFIX_AGENT_COMMANDS
    ):
        return [body[1], *body[2:]], body[0]
    return body, None


def _looks_like_agent_name(token: str) -> bool:
    return bool(token) and not token.startswith("-") and token not in TOP_LEVEL_COMMANDS


if __name__ == "__main__":
    raise SystemExit(main())
