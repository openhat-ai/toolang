"""Foreground and background agent runtime commands."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import sys
from typing import Annotated, TYPE_CHECKING

import click
import typer

from toolang.common.layout import AgentLayout
from toolang.up import process as agents
from toolang.state.state import split_cap_selectors
from toolang.plugin.models.resolution import split_model_selectors
from toolang.plugin.sandboxes.loading import create_sandbox_plugin
from toolang.plugin.tools.registry import split_tool_selectors
from ....up.logging import (
    LoggingPlan,
    configure_logging_plan,
    resolve_agent_logging,
)
from ...common.context import (
    context_root,
    require_runtime_agent,
    load_runtime_environ,
    ui_base_url,
    user_call,
)
from ...common.output import active_agent_error

if TYPE_CHECKING:
    from toolang.up import server as agent_up
    from ....state.state import AgentState
    from ...common.progress import CliProgress


@dataclass(frozen=True, slots=True)
class RuntimeStartup:
    target: AgentLayout
    startup: agent_up.StartupSpec
    environ: dict[str, str]
    log_plan: LoggingPlan
    agent_state: AgentState


@dataclass(frozen=True, slots=True)
class _RoamingFileOptions:
    inboxes: tuple[Path, ...]
    models: tuple[str, ...]
    tools: tuple[str, ...] | None
    caps: tuple[str, ...]
    host: str
    endpoint_host: str | None
    port: int | None
    sandbox: str
    dev: Path | None


def is_roaming_file_request(args: list[str]) -> bool:
    return bool(args and args[0].startswith("-")) and any(
        token == "--inbox" or token.startswith("--inbox=") for token in args
    )


def run_roaming_file(source: Path, args: list[str]) -> int:
    from toolang.up import server as up
    from ...common.context import load_runtime_environ

    try:
        options = _parse_roaming_file_options(args)
        layout = agents.materialize_roaming_program(source)
        existing = agents.AgentProcess(layout).status(ui_base_url=ui_base_url())
        if existing is not None and existing.status in {
            "running",
            "preparing",
            "starting",
        }:
            raise click.ClickException(active_agent_error(existing))
        environ = load_runtime_environ(layout, base_environ=os.environ)
        environ["TOOLANG_ROOT"] = str(layout.root)
        log_plan = resolve_agent_logging(
            mode="run",
            environ=environ,
            agent_log_path=layout.runtime_log,
        )
        configure_logging_plan(log_plan)
        startup = user_call(
            up.resolve_startup,
            layout=layout,
            host=options.host,
            endpoint_host=options.endpoint_host,
            port=options.port,
            sandbox=options.sandbox,
            models=options.models,
            tools=options.tools,
            caps=options.caps,
            file_inboxes=options.inboxes,
            dev=options.dev,
            log_spec=log_plan.spec,
            temporary_port=options.port is None,
            environ=log_plan.environ,
        )
        state = user_call(up.prepare_agent, layout=layout)
        return user_call(
            up.start_runtime,
            startup,
            environ=log_plan.environ,
            agent_state=state,
        )
    except KeyboardInterrupt:
        return 130
    except (
        FileExistsError,
        FileNotFoundError,
        ValueError,
        click.ClickException,
    ) as exc:
        message = exc.message if isinstance(exc, click.ClickException) else str(exc)
        typer.echo(f"toolang error: {message}", err=True)
        return 1


def _parse_roaming_file_options(argv: list[str]) -> _RoamingFileOptions:
    inboxes: list[Path] = []
    models: list[str] = []
    tools: list[str] | None = None
    caps: list[str] = []
    host = "127.0.0.1"
    endpoint_host: str | None = None
    port: int | None = None
    sandbox = "none"
    dev: Path | None = None
    index = 0
    while index < len(argv):
        token = argv[index]
        option, separator, inline = token.partition("=")
        if option in {
            "--inbox",
            "--models",
            "--tools",
            "--caps",
            "--host",
            "--endpoint-host",
            "--port",
            "--sandbox",
            "--dev",
        }:
            value = inline.strip() if separator else _option_value(argv, index, option)
            if not value and option != "--endpoint-host":
                raise click.ClickException(f"{option} requires a value")
            index += 1 if separator else 2
            if option == "--inbox":
                inboxes.append(Path(value))
            elif option == "--models":
                models.extend(split_model_selectors((value,)))
            elif option == "--tools":
                tools = [] if tools is None else tools
                tools.extend(split_tool_selectors((value,)))
            elif option == "--caps":
                caps.extend(split_cap_selectors((value,)))
            elif option == "--host":
                host = value
            elif option == "--endpoint-host":
                endpoint_host = value or None
            elif option == "--port":
                try:
                    port = int(value)
                except ValueError as exc:
                    raise click.ClickException("--port expects an integer") from exc
            elif option == "--sandbox":
                sandbox = value
            elif option == "--dev":
                dev = Path(value)
            continue
        if token in {"--help", "-h"}:
            raise click.ClickException(
                "file request runtime usage: toolang SCRIPT --inbox PATH [--inbox PATH...]"
            )
        if token.startswith("-"):
            raise click.ClickException(f"unknown Toolang runtime option: {token}")
        raise click.ClickException(
            f"unexpected agic argument for file request runtime: {token}"
        )
    if not inboxes:
        raise click.ClickException("--inbox is required")
    return _RoamingFileOptions(
        inboxes=tuple(inboxes),
        models=tuple(dict.fromkeys(models)),
        tools=None if tools is None else tuple(dict.fromkeys(tools)),
        caps=tuple(dict.fromkeys(caps)),
        host=host,
        endpoint_host=endpoint_host,
        port=port,
        sandbox=sandbox,
        dev=dev,
    )


def _option_value(argv: list[str], index: int, option: str) -> str:
    if index + 1 >= len(argv) or not argv[index + 1].strip():
        raise click.ClickException(f"{option} requires a value")
    return argv[index + 1].strip()


def run(
    ctx: typer.Context,
    agent: str | None = typer.Argument(
        None,
        help="Existing local agent name, remote agent ref, or URL.",
        hidden=True,
    ),
    sandbox: Annotated[
        str | None,
        typer.Option(help="Run in this sandbox; defaults to agent config or none."),
    ] = None,
    models: Annotated[
        list[str] | None,
        typer.Option("--models", help="Limit available models. Pass CSV or repeat."),
    ] = None,
    tools: Annotated[
        list[str] | None,
        typer.Option("--tools", help="Allow selected tools. Pass CSV or repeat."),
    ] = None,
    caps: Annotated[
        list[str] | None,
        typer.Option("--caps", help="Allow selected caps. Pass CSV or repeat."),
    ] = None,
    host: Annotated[
        str, typer.Option(help="Bind the agent API to this host.")
    ] = "127.0.0.1",
    port: Annotated[
        int | None, typer.Option(help="Bind the agent API to this port.")
    ] = None,
    inboxes: Annotated[
        list[Path] | None,
        typer.Option(
            "--inbox",
            help="Watch an inbox directory for file requests. Repeat to watch more than one.",
        ),
    ] = None,
    dev: Annotated[
        Path | None,
        typer.Option(
            "--dev",
            help="Use wheels from this file or directory when starting a sandbox.",
        ),
    ] = None,
    endpoint_host: Annotated[
        str | None,
        typer.Option("--endpoint-host", help="Endpoint host name.", hidden=True),
    ] = None,
    sandbox_child: Annotated[
        bool, typer.Option("--sandbox-child", hidden=True)
    ] = False,
    background_hosting: Annotated[
        bool, typer.Option("--background-hosting", hidden=True)
    ] = False,
) -> None:
    from toolang.up import server as up
    from ...common.progress import as_progress_sink, make_cli_progress

    selector = require_runtime_agent(ctx, agent)
    progress = make_cli_progress()
    finished = False
    try:
        target = agents.resolve_run_layout(
            context_root(ctx),
            selector,
            progress=as_progress_sink(progress),
        )
        launch = resolve_startup(
            ctx,
            target,
            sandbox=sandbox,
            models=models,
            tools=tools,
            caps=caps,
            inboxes=inboxes,
            port=port,
            host=host,
            endpoint_host=endpoint_host,
            dev=dev,
            background=False,
            progress=progress,
        )
        progress.finish(details=False)
        finished = True
        raise typer.Exit(
            user_call(
                up.start_runtime,
                launch.startup,
                environ=launch.environ,
                sandbox_child=sandbox_child,
                progress=None,
                agent_state=launch.agent_state,
                wait=not background_hosting,
            )
        )
    except KeyboardInterrupt:
        if not finished:
            progress.interrupt()
        raise typer.Exit(130) from None
    except (
        FileExistsError,
        FileNotFoundError,
        ValueError,
        click.ClickException,
    ) as exc:
        if not finished:
            progress.finish(details=False)
        if isinstance(exc, click.ClickException):
            raise
        raise click.ClickException(str(exc)) from exc


def start(
    ctx: typer.Context,
    agent: str | None = typer.Argument(
        None, help="Existing local agent name.", hidden=True
    ),
    sandbox: Annotated[
        str | None,
        typer.Option(help="Run in this sandbox; defaults to agent config or none."),
    ] = None,
    models: Annotated[
        list[str] | None,
        typer.Option("--models", help="Limit available models. Pass CSV or repeat."),
    ] = None,
    tools: Annotated[
        list[str] | None,
        typer.Option("--tools", help="Allow selected tools. Pass CSV or repeat."),
    ] = None,
    caps: Annotated[
        list[str] | None,
        typer.Option("--caps", help="Allow selected caps. Pass CSV or repeat."),
    ] = None,
    host: Annotated[
        str, typer.Option(help="Bind the agent API to this host.")
    ] = "127.0.0.1",
    port: Annotated[
        int | None, typer.Option(help="Bind the agent API to this port.")
    ] = None,
    inboxes: Annotated[
        list[Path] | None,
        typer.Option(
            "--inbox",
            help="Watch an inbox directory for file requests. Repeat to watch more than one.",
        ),
    ] = None,
    dev: Annotated[
        Path | None,
        typer.Option(
            "--dev",
            help="Use wheels from this file or directory when starting a sandbox.",
        ),
    ] = None,
    endpoint_host: Annotated[
        str | None,
        typer.Option("--endpoint-host", help="Endpoint host name.", hidden=True),
    ] = None,
) -> None:
    from toolang.up import server as up
    from ...common.progress import as_progress_sink, make_cli_progress

    selector = require_runtime_agent(ctx, agent)
    if user_call(agents.parse_agent_selector, selector).form != "name":
        raise click.ClickException(
            "start only supports local agent names; clone the remote source first"
        )
    progress = make_cli_progress()
    try:
        target = agents.resolve_run_layout(
            context_root(ctx),
            selector,
            progress=as_progress_sink(progress),
        )
        launch = resolve_startup(
            ctx,
            target,
            sandbox=sandbox,
            models=models,
            tools=tools,
            caps=caps,
            inboxes=inboxes,
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

    progress.finish(details=False)
    if launch.log_plan.path is None:
        raise click.ClickException("agent log path was not resolved")
    log_path = launch.log_plan.path
    command = [
        sys.executable,
        "-m",
        "toolang.cli.toolang",
        *up.build_run_argv(launch.startup, background_hosting=True),
    ]
    try:
        status = agents.AgentProcess(launch.target).start(
            command,
            environ=launch.environ,
            cwd=Path.cwd(),
            log_path=log_path,
            ui_base_url=ui_base_url(),
        )
    except RuntimeError as exc:
        raise click.ClickException(
            f"Agent {launch.target.name} failed to start: {log_path}"
        ) from exc
    except TimeoutError as exc:
        raise click.ClickException(
            f"Agent {launch.target.name} start timed out: {log_path}"
        ) from exc
    if status.status == "failed":
        raise click.ClickException(
            f"Agent {launch.target.name} failed to start: {log_path}"
        )
    typer.echo(
        f"Started agent {launch.target.name}: "
        f"{status.webui_url or status.api_url or status.endpoint or '-'}"
    )


def stop(
    ctx: typer.Context,
    agent: str | None = typer.Argument(None, help="Agent name", hidden=True),
    force: Annotated[
        bool,
        typer.Option(help="Force-stop when graceful shutdown does not complete."),
    ] = False,
) -> None:
    agent_name = require_runtime_agent(ctx, agent)
    root = context_root(ctx)
    process = agents.AgentProcess(AgentLayout.resident(root, agent_name))
    runtime_state = process.state()
    runtime_pids = () if runtime_state is not None else process.pids()
    if runtime_state is None and not runtime_pids:
        raise click.ClickException(f"Agent {agent_name} not running")

    sandbox_plugin = None
    sandbox = runtime_state.get("sandbox") if runtime_state is not None else None
    if isinstance(sandbox, dict):
        selector = {str(key): value for key, value in sandbox.items()}.get("selector")
        if not isinstance(selector, dict):
            raise click.ClickException(f"Sandbox state is invalid for agent: {agent}")
        driver = {str(key): value for key, value in selector.items()}.get("driver")
        if not isinstance(driver, str) or not driver.strip():
            raise click.ClickException(
                f"Sandbox driver is missing for agent: {agent_name}"
            )
        sandbox_plugin = create_sandbox_plugin(driver.strip(), config={})

    stopped = user_call(
        process.stop,
        sandbox_plugin=sandbox_plugin,
        force=force,
    )
    typer.echo(
        f"Stopped agent {agent_name}" if stopped else f"Agent {agent_name} not running"
    )


def resolve_startup(
    ctx: typer.Context,
    target: AgentLayout,
    *,
    sandbox: str | None,
    models: list[str] | None,
    tools: list[str] | None,
    caps: list[str] | None,
    inboxes: list[Path] | None,
    port: int | None,
    host: str,
    endpoint_host: str | None,
    dev: Path | None,
    background: bool,
    progress: CliProgress | None,
) -> RuntimeStartup:
    from toolang.up import server as up
    from ...common.progress import as_progress_sink

    root, agent = target.root, target.name
    if target.placement == "resident" and not target.home.is_dir():
        raise click.ClickException(f"Agent {agent} not found")
    existing = agents.AgentProcess(target).status(ui_base_url=ui_base_url())
    if existing is not None and existing.status in {"running", "preparing", "starting"}:
        raise click.ClickException(active_agent_error(existing))
    environ = load_runtime_environ(target, base_environ=os.environ)
    environ["TOOLANG_ROOT"] = str(root)
    log_plan = resolve_agent_logging(
        mode="start" if background else "run",
        environ=environ,
        agent_log_path=target.runtime_log,
    )
    if not background:
        configure_logging_plan(log_plan)
    agent_state = user_call(
        up.prepare_agent,
        layout=target,
        progress=as_progress_sink(progress),
    )
    startup = user_call(
        up.resolve_startup,
        layout=target,
        host=host,
        endpoint_host=endpoint_host,
        port=port,
        sandbox=sandbox,
        models=models,
        tools=tools,
        caps=caps,
        file_inboxes=inboxes,
        dev=dev,
        log_spec=log_plan.spec,
        temporary_port=target.placement == "visiting" and port is None,
        environ=log_plan.environ,
        agent_state=agent_state,
    )
    return RuntimeStartup(target, startup, log_plan.environ, log_plan, agent_state)
