"""Typer CLI for Toolang agent management."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
import os
import subprocess
import sys
import time
from typing import Annotated, Literal, cast

import click
import typer
from typer.core import TyperCommand

from .. import agents, caps as cap_store, templates, work
from .. import up as agent_up
from ..base.protocols.model import ModelProvider
from ..base.types.model import ModelInfo
from ..config.log import DEFAULT_AGENT_LOG_SPEC, configure_logging
from ..config.log_spec import PY_LOG_ENV_VAR
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
from .utils import (
    _AGENT_AVATAR,
    _OptionalTemplateArgumentCommand,
    _RequiredPrefixAgentCommand,
    _RunAgentCommand,
    _RuntimeAgentCommand,
    _append_agent_update,
    _context_root,
    _created_time,
    _echo_pairs_table,
    _echo_table,
    _format_runtime_row,
    _make_template_list_command,
    _make_template_show_command,
    _normalize_loop_option,
    _parse_utc_timestamp,
    _required_prefix_agent,
    _required_runtime_agent,
    _runtime_environ_for_agent,
    _runtime_loops,
    _runtime_value,
    _toolang_root,
    _ui_base_url,
    _wait_for_started_status,
    _wrap_user_error,
)

WorkKind = Literal["task", "chore"]
_CLI_PREFIX_AGENT: str | None = None
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
    log: Annotated[
        str | None,
        typer.Option(
            "--log",
            help="Set logging directives. Uses PY_LOG when omitted.",
        ),
    ] = None,
) -> None:
    """Toolang CLI."""

    try:
        configure_logging(spec=log, environ=os.environ)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    ctx.obj = {
        "toolang_root": _toolang_root(toolang_root),
        "agent": _CLI_PREFIX_AGENT,
        "log": log,
    }


@app.command("new", help="Create an agent.", no_args_is_help=True)
def new_agent(
    ctx: typer.Context,
    agent: Annotated[str, typer.Argument(help="Agent name")],
    template: Annotated[
        str,
        typer.Option("--template", "-t", help="Template name."),
    ] = "default",
) -> None:
    program_path = _wrap_user_error(
        agents.create_agent,
        _context_root(ctx),
        agent,
        template_name=template,
    )
    _append_agent_update(
        _context_root(ctx),
        agent,
        "created",
        {"path": str(program_path)},
    )
    typer.echo(str(program_path))


@app.command("clone", help="Clone an agent.", no_args_is_help=True)
def clone_agent(
    ctx: typer.Context,
    source: Annotated[str, typer.Argument(help="Agent source selector.")],
    target: Annotated[str | None, typer.Argument(help="New local agent name.")] = None,
) -> None:
    program_path = _wrap_user_error(agents.clone_agent, _context_root(ctx), source, target)
    target_name = program_path.stem
    _append_agent_update(
        _context_root(ctx),
        target_name,
        "created",
        {"path": str(program_path), "source": source},
    )
    typer.echo(str(program_path))


@app.command("remove", help="Remove an agent.", no_args_is_help=True)
def remove_agent(
    ctx: typer.Context,
    agent: Annotated[str, typer.Argument(help="Agent name")],
) -> None:
    _wrap_user_error(agents.remove_agent, _context_root(ctx), agent)
    typer.echo(f"{agent}\tremoved")


@app.command("list", help="Show agents and their status.")
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


@app.command("info", help="Show agent info.", no_args_is_help=True, cls=_RuntimeAgentCommand)
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
        raise click.ClickException(f"agent not found: {agent_name}")
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
        loops_text = _runtime_loops(runtime_state)
        if loops_text is not None:
            rows.append(("Loops", loops_text))
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
    loops: Annotated[
        list[str] | None,
        typer.Option("--loop", help="Runtime loop to enable. Repeat or pass CSV."),
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
    public_host: Annotated[
        str | None,
        typer.Option("--public-host", help="Published host name.", hidden=True),
    ] = None,
    sandbox_child: Annotated[
        bool,
        typer.Option("--sandbox-child", hidden=True),
    ] = False,
) -> None:
    selector = _required_runtime_agent(ctx, agent)
    normalized_loops = _normalize_loop_option(loops)
    root = _context_root(ctx)
    try:
        with agents.materialized_run_target(root, selector) as (run_root, agent_name):
            raise typer.Exit(
                _wrap_user_error(
                    agent_up.up,
                    toolang_root=run_root,
                    agent_name=agent_name,
                    host=host,
                    public_host=public_host,
                    port=port,
                    sandbox=sandbox,
                    models=models,
                    dev=dev,
                    sandbox_child=sandbox_child,
                    loop_names=normalized_loops,
                    environ=_runtime_environ_for_agent(ctx, agent_name, toolang_root=run_root),
                )
            )
    except (FileExistsError, FileNotFoundError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc


@app.command(
    "start",
    help="Start an agent.",
    no_args_is_help=True,
    cls=_RuntimeAgentCommand,
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
    loops: Annotated[
        list[str] | None,
        typer.Option("--loop", help="Runtime loop to enable. Repeat or pass CSV."),
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
    public_host: Annotated[
        str | None,
        typer.Option("--public-host", help="Published host name.", hidden=True),
    ] = None,
) -> None:
    selector = _required_runtime_agent(ctx, agent)
    parsed_selector = _wrap_user_error(agents.parse_agent_selector, selector)
    if parsed_selector.form != "name":
        raise click.ClickException("start only supports local agent names; clone the remote source first")
    agent_name = parsed_selector.name or ""
    root = _context_root(ctx)
    normalized_loops = _normalize_loop_option(loops)
    existing = agents.get_agent_status(root, agent_name, ui_base_url=_ui_base_url())
    if existing is not None and existing.status in {"running", "preparing", "starting"}:
        raise click.ClickException(f"agent is already active: {agent_name}")

    environ = _runtime_environ_for_agent(ctx, agent_name)
    environ["TOOLANG_ROOT"] = str(root)
    explicit_log_spec = cast(str | None, ctx.obj.get("log"))
    if explicit_log_spec is None and not environ.get(PY_LOG_ENV_VAR, "").strip():
        environ[PY_LOG_ENV_VAR] = DEFAULT_AGENT_LOG_SPEC
    startup = _wrap_user_error(
        agent_up.resolve_startup,
        toolang_root=root,
        agent_name=agent_name,
        host=host,
        public_host=public_host,
        port=port,
        sandbox=sandbox,
        models=models,
        dev=dev,
        loop_names=normalized_loops,
        log_spec=explicit_log_spec,
        environ=environ,
    )
    log_path = agents.agent_runtime_log_path(root, agent_name)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    launched_at = time.time()
    command = [
        sys.executable,
        "-m",
        "toolang.cli.main",
        *agent_up.build_run_argv(startup),
    ]
    with log_path.open("ab") as stream:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=stream,
            stderr=stream,
            env=environ,
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
            raise click.ClickException(f"agent failed during startup: {agent_name} (see {log_path})")
        raise click.ClickException(f"agent start timed out: {agent_name} (see {log_path})")
    if status.status == "failed":
        raise click.ClickException(f"{agent_name}\tfailed\t{status.endpoint or '-'}\t{log_path}")
    typer.echo(_format_runtime_row(status))


@app.command(
    "stop",
    help="Stop an agent.",
    no_args_is_help=True,
    cls=_RuntimeAgentCommand,
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
    runtime_state = agents.load_runtime_state(_context_root(ctx), agent_name)
    if runtime_state is None:
        raise click.ClickException(f"Agent is not running: {agent_name}")

    sandbox_plugin = None
    sandbox = runtime_state.get("sandbox")
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
        _context_root(ctx),
        agent_name,
        sandbox_plugin=sandbox_plugin,
        force=force,
    )
    typer.echo(f"{agent_name}\tstopped" if stopped else f"{agent_name}\talready-stopped")

def _info_caps_summary(toolang_root: Path, agent_name: str) -> str:
    counts = {
        "skills": len(cap_store.list_entries(toolang_root, agent_name, scope=None, kinds={"skill"})),
        "psyches": len(cap_store.list_entries(toolang_root, agent_name, scope=None, kinds={"psyche"})),
        "services": len(cap_store.list_entries(toolang_root, agent_name, scope=None, kinds={"service"})),
        "prompts": len(cap_store.list_entries(toolang_root, agent_name, scope=None, kinds={"prompt"})),
    }
    singular = {
        "skills": "skill",
        "psyches": "psyche",
        "services": "service",
        "prompts": "prompt",
    }
    parts = [
        f"{count} {singular[label] if count == 1 else label}"
        for label, count in counts.items()
        if count
    ]
    return ", ".join(parts) if parts else "0"


def _info_jobs_summary(toolang_root: Path, agent_name: str) -> str:
    chore_count = len(work.list_chores(toolang_root, agent_name))
    task_count = len(work.list_tasks(toolang_root, agent_name))
    parts = []
    if chore_count:
        parts.append(f"{chore_count} {'chore' if chore_count == 1 else 'chores'}")
    if task_count:
        parts.append(f"{task_count} {'task' if task_count == 1 else 'tasks'}")
    return ", ".join(parts) if parts else "0"


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
    _echo_table(("FAMILY", "NAME", "STATUS", "DETAILS"), rows)


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


def _plugin_rows(environ: dict[str, str]) -> list[tuple[str, str, str, str]]:
    rows: list[tuple[str, str, str, str]] = []
    for name, provider in sorted(agent_up.load_model_providers().items()):
        rows.append(
            (
                "model",
                name,
                _model_provider_status(provider, environ=environ),
                _model_provider_details(provider, environ=environ),
            )
        )
    rows.extend(
        ("tool", name, "installed", "Installed plugin entry point.")
        for name in agent_up.list_plugin_names(group="toolang.tool")
    )
    rows.extend(
        ("channel", name, "installed", "Installed plugin entry point.")
        for name in agent_up.list_plugin_names(group="toolang.channel")
    )
    rows.extend(
        ("sandbox", name, "installed", "Installed plugin entry point.")
        for name in agent_up.list_plugin_names(group="toolang.sandbox")
    )
    return rows


def _model_provider_status(
    provider: ModelProvider,
    *,
    environ: dict[str, str],
) -> str:
    missing = missing_provider_env_vars(provider, environ=environ)
    return "missing-env" if missing else "ready"


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
    name: str,
    path: Path,
) -> None:
    update_kind = cast(UpdateKind, f"{kind}_changed")
    _append_agent_update(
        toolang_root,
        agent_name,
        update_kind,
        {
            "name": name,
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
            no_args_is_help=True,
        ),
        WorkCommandSpec(
            name="edit",
            help=lambda kind: f"Edit a {kind}.",
            factory=_make_edit_work_command,
            cls=_RequiredPrefixAgentCommand,
            no_args_is_help=True,
        ),
        WorkCommandSpec(
            name="remove",
            help=lambda kind: f"Remove a {kind}.",
            factory=_make_remove_work_command,
            cls=_RequiredPrefixAgentCommand,
            no_args_is_help=True,
        ),
        WorkCommandSpec(
            name="templates",
            help=lambda kind: f"List {kind} templates.",
            factory=lambda kind, title: _make_template_list_command(kind, title=title),
        ),
        WorkCommandSpec(
            name="template",
            help=lambda kind: f"Show a {kind} template.",
            factory=lambda kind, title: _make_template_show_command(kind, title=title),
            cls=_OptionalTemplateArgumentCommand,
        ),
    )

    for kind in work_titles:
        title = work_titles[kind]
        work_app = typer.Typer(
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
        app.add_typer(work_app, name=kind, no_args_is_help=True)


def _make_work_list_command(kind: WorkKind, title: str) -> Callable[..., None]:
    def list_work(ctx: typer.Context) -> None:
        agent_name = _required_prefix_agent(ctx, command_name=kind)
        if kind == "task":
            entries = work.list_tasks(_context_root(ctx), agent_name)
            if not entries:
                typer.echo("No tasks found.")
                return
            rows = [
                (
                    entry.name,
                    entry.document.status,
                    entry.document.requester,
                    "yes" if entry.document.paused else "-",
                    str(entry.path),
                )
                for entry in entries
            ]
            _echo_table((title.upper(), "STATUS", "REQUESTER", "PAUSED", "LOCATION"), rows)
            return
        entries = work.list_chores(_context_root(ctx), agent_name)
        if not entries:
            typer.echo("No chores found.")
            return
        rows = [
            (
                entry.name,
                (entry.document.title or "-").strip() or "-",
                entry.document.rrule,
                "yes" if entry.document.paused else "-",
                str(entry.path),
            )
            for entry in entries
        ]
        _echo_table((title.upper(), "TITLE", "RRULE", "PAUSED", "LOCATION"), rows)

    return list_work


def _make_new_work_command(kind: WorkKind, title: str) -> Callable[..., None]:
    def new_work(
        ctx: typer.Context,
        name: str = typer.Argument(..., help=f"{title} name"),
        template: Annotated[
            str,
            typer.Option("--template", "-t", help="Template name."),
        ] = "default",
    ) -> None:
        agent_name = _required_prefix_agent(ctx, command_name=kind)
        text = click.edit(
            templates.render_template(kind, template, name=name, agent_name=agent_name),
            extension=".md",
            require_save=True,
        )
        if text is None:
            raise typer.Exit()
        path = _wrap_user_error(
            work.put_task_text if kind == "task" else work.put_chore_text,
            _context_root(ctx),
            agent_name,
            name,
            text,
        )
        _append_work_update(_context_root(ctx), agent_name, kind=kind, name=name, path=path)
        typer.echo(str(path))

    return new_work


def _make_edit_work_command(kind: WorkKind, title: str) -> Callable[..., None]:
    def edit_work(
        ctx: typer.Context,
        name: str = typer.Argument(..., help=f"{title} name"),
    ) -> None:
        agent_name = _required_prefix_agent(ctx, command_name=kind)
        text = _wrap_user_error(
            work.load_task_text if kind == "task" else work.load_chore_text,
            _context_root(ctx),
            agent_name,
            name,
        )
        updated_text = click.edit(
            text,
            extension=".md",
            require_save=True,
        )
        if updated_text is None:
            raise typer.Exit()
        path = _wrap_user_error(
            work.put_task_text if kind == "task" else work.put_chore_text,
            _context_root(ctx),
            agent_name,
            name,
            updated_text,
        )
        _append_work_update(_context_root(ctx), agent_name, kind=kind, name=name, path=path)
        typer.echo(str(path))

    return edit_work


def _make_remove_work_command(kind: WorkKind, title: str) -> Callable[..., None]:
    def remove_work(
        ctx: typer.Context,
        name: str = typer.Argument(..., help=f"{title} name"),
    ) -> None:
        agent_name = _required_prefix_agent(ctx, command_name=kind)
        path = (
            work.task_path(_context_root(ctx), agent_name, name)
            if kind == "task"
            else work.chore_path(_context_root(ctx), agent_name, name)
        )
        removed = _wrap_user_error(
            work.remove_task if kind == "task" else work.remove_chore,
            _context_root(ctx),
            agent_name,
            name,
        )
        if not removed:
            raise click.ClickException(f"{kind} not found: {name}")
        _append_work_update(_context_root(ctx), agent_name, kind=kind, name=name, path=path)
        typer.echo(f"Removed {kind} {name} from {path}")

    return remove_work

caps_cli.register_cap_commands(app)
app.add_typer(model_app, name="model", no_args_is_help=True)
app.add_typer(plugin_app, name="plugin", no_args_is_help=True)
register_work_commands()


def main(argv: Sequence[str] | None = None) -> int:
    global _CLI_PREFIX_AGENT
    raw_args = list(argv) if argv is not None else sys.argv[1:]
    global_args, body = _extract_global_args(raw_args)
    log_spec = _global_log_spec(global_args)
    try:
        configure_logging(spec=log_spec, environ=os.environ)
    except ValueError as exc:
        typer.echo(f"toolang error: {exc}", err=True)
        return 1
    if body:
        roaming_source = cli_invoke.roaming_source_path(body[0])
        if roaming_source is not None:
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
    if token == "--log":
        if index + 1 >= len(argv):
            return ([token], 1)
        return ([token, argv[index + 1]], 2)
    if token.startswith("--log="):
        return (["--log", token.removeprefix("--log=")], 1)
    return None


def _global_log_spec(global_args: list[str]) -> str | None:
    index = 0
    value: str | None = None
    while index < len(global_args):
        token = global_args[index]
        if token == "--log":
            if index + 1 >= len(global_args):
                return None
            value = global_args[index + 1]
            index += 2
            continue
        if token == "--root":
            index += 2
            continue
        index += 1
    return value


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
