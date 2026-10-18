"""Foreground and background AgentServer commands."""

from __future__ import annotations

import asyncio
from contextlib import ExitStack
from dataclasses import dataclass
import os
import sys
import json
from pathlib import Path
from typing import Annotated, TYPE_CHECKING
from toolang.api.version import AGENT_API_VERSION
from toolang.cli.common.workspaces import (
    WorkspaceOptions,
    WorkdirOption,
    resolve_workspaces,
    single_workdir,
)

from toolang.cli.common.terminal_session import prepare_terminal
from toolang.cli.common.terminal_title import agent_label

import typer
from typer._click.exceptions import ClickException
from typer._click.core import ParameterSource

from toolang.cli.common.parameters import (
    AllowOptions,
    CompactModelOption,
    DriverOption,
    DefaultOptions,
    LimitOptions,
    TextType,
)

from toolang.common.layout import AgentLayout
from toolang.up.types import ServerLifetime
from toolang.up.config import format_drivers, parse_drivers
from toolang.cli.common.agent_server import (
    acquire_agent_server,
    AgentServerAcquisitionError,
    ServerClosingError,
)

import httpx

from toolang.plugin.catalogs.models_dev.path import MODEL_CATALOG_ENV
from toolang.cli.common.policy import (
    resolve_default_overrides,
    resolve_compact_override,
    resolve_ceiling_overrides,
    resolve_limit_overrides,
)
from toolang.up import source as agent_sources
from toolang.up import process as agents
from ....up.logging import (
    LoggingPlan,
    configure_logging_plan,
    resolve_agent_logging,
)
from ...common.context import (
    ModelCatalogOption,
    cli_context,
    context_root,
    require_runtime_agent,
    load_runtime_environ,
    resolve_model_catalog_option,
    ui_base_url,
    user_call,
)
from ...common.agent_server import (
    DEVELOPMENT_WHEEL_HELP,
    warn_development_package_source,
)
from ...common.output import active_agent_error
from toolang.common.version import development_source

if TYPE_CHECKING:
    from toolang.up.instance import SandboxState
    from toolang.up.sandbox import LaunchSpec

from ...common.progress import CliProgress, runtime_startup_failure_message


@dataclass(frozen=True, slots=True)
class RuntimeLaunch:
    target: AgentLayout
    startup: LaunchSpec
    environ: dict[str, str]
    log_plan: LoggingPlan


def run(
    ctx: typer.Context,
    agent: str | None = typer.Argument(
        None,
        help="Agent name, .too path, GitHub ref, or URL",
        hidden=True,
    ),
    sandbox: Annotated[
        str | None,
        typer.Option(
            "--sandbox",
            metavar="SANDBOX",
            help="Run in this sandbox; defaults to config, last successful selection, or host",
        ),
    ] = None,
    allows: AllowOptions = None,
    limits: LimitOptions = None,
    defaults: DefaultOptions = None,
    compact_model: CompactModelOption = None,
    driver: DriverOption = None,
    model_catalog: ModelCatalogOption = None,
    host: Annotated[
        str,
        typer.Option("--host", metavar="HOST", help="Bind the agent API to this host"),
    ] = "127.0.0.1",
    port: Annotated[
        int | None,
        typer.Option("--port", metavar="PORT", help="Bind the agent API to this port"),
    ] = None,
    workspace: WorkspaceOptions = None,
    workdir: WorkdirOption = None,
    dev: Annotated[
        Path | None,
        typer.Option("--dev", metavar="[PATH]", help=DEVELOPMENT_WHEEL_HELP),
    ] = None,
    endpoint_host: Annotated[
        str | None,
        typer.Option("--endpoint-host", help="Endpoint host name", hidden=True),
    ] = None,
) -> None:
    from toolang.up import sandbox as sandbox_runtime
    from ...common.progress import make_cli_progress

    selector = require_runtime_agent(ctx, agent)
    progress = make_cli_progress(preparation=True)
    cleanup_progress = make_cli_progress(leading_gap=True)
    launch: RuntimeLaunch | None = None
    launch_started = False
    try:
        with ExitStack() as terminal_scope, progress, cleanup_progress:
            selected_layout = cli_context(ctx).layout
            target = (
                selected_layout
                if selected_layout is not None
                else agent_sources.resolve_run_layout(
                    context_root(ctx),
                    selector,
                    progress=progress.sink,
                )
            )
            terminal_scope.enter_context(
                prepare_terminal(
                    "serve",
                    layout=target,
                    label=f"serve {agent_label(target.name)}",
                    stream=sys.stderr,
                )
            )
            launch = resolve_startup(
                target,
                toolang_root=context_root(ctx),
                model_catalog=model_catalog,
                sandbox=sandbox,
                allows=allows,
                defaults=defaults,
                compact_model=compact_model,
                driver=driver,
                limits=limits,
                port=port,
                host=host,
                endpoint_host=endpoint_host,
                dev=dev,
                background=False,
                workspace_options=workspace,
                workdir=single_workdir(workdir),
            )
            with progress.suspended():
                warn_development_package_source(launch.startup)
            launch_started = True
            if launch.startup.sandbox == "host":
                from toolang.up.server import serve as serve_agent
                from dataclasses import replace
                from toolang.plugin.sandboxes.host import (
                    host_sandbox_description,
                    HOST_SANDBOX_DESCRIPTION_ENV,
                )

                environ = dict(launch.environ)
                environ[HOST_SANDBOX_DESCRIPTION_ENV] = host_sandbox_description()
                user_call(
                    sandbox_runtime.prepare_launch_state,
                    launch.startup,
                    progress=progress.sink,
                )
                exit_code = user_call(
                    serve_agent,
                    replace(launch.startup.serve, toolang_root=context_root(ctx)),
                    environ=environ,
                    sandbox="host",
                )
            else:
                exit_code = user_call(
                    asyncio.run,
                    sandbox_runtime.run(
                        launch.startup,
                        on_ready=lambda state: _report_foreground_ready(
                            launch.target.name, state, progress
                        ),
                        progress=progress.sink,
                        cleanup_progress=cleanup_progress.sink,
                    ),
                )
    except KeyboardInterrupt:
        raise typer.Exit(130) from None
    except (
        FileExistsError,
        FileNotFoundError,
        OSError,
        RuntimeError,
        ValueError,
        ClickException,
    ) as exc:
        if isinstance(exc, ClickException) and not launch_started:
            raise
        if launch_started and launch is not None:
            if cleanup_progress.failure_stage is not None:
                message = cleanup_progress.failure_message(
                    exc,
                    log_path=launch.target.runtime_log,
                )
            else:
                message = runtime_startup_failure_message(
                    progress,
                    exc,
                    dev_artifact=launch.startup.dev_artifact,
                    development_build=development_source()[0],
                )
            raise ClickException(message) from exc
        raise ClickException(str(exc)) from exc
    raise typer.Exit(exit_code)


def _report_foreground_ready(
    name: str,
    state: SandboxState,
    progress: CliProgress,
) -> None:
    progress.close()
    typer.echo(
        f"Agent {name} running: {state.ref.endpoint} (Ctrl+C to stop)",
        err=True,
    )


def start(
    ctx: typer.Context,
    agent: str | None = typer.Argument(
        None, help="Agent Name or remote Source Ref", hidden=True
    ),
    sandbox: Annotated[
        str | None,
        typer.Option(
            "--sandbox",
            metavar="SANDBOX",
            help="Run in this sandbox; defaults to config, last successful selection, or host",
        ),
    ] = None,
    allows: AllowOptions = None,
    limits: LimitOptions = None,
    defaults: DefaultOptions = None,
    compact_model: CompactModelOption = None,
    driver: DriverOption = None,
    model_catalog: ModelCatalogOption = None,
    host: Annotated[
        str,
        typer.Option("--host", metavar="HOST", help="Bind the agent API to this host"),
    ] = "127.0.0.1",
    port: Annotated[
        int | None,
        typer.Option("--port", metavar="PORT", help="Bind the agent API to this port"),
    ] = None,
    dev: Annotated[
        Path | None,
        typer.Option("--dev", metavar="[PATH]", help=DEVELOPMENT_WHEEL_HELP),
    ] = None,
    endpoint_host: Annotated[
        str | None,
        typer.Option("--endpoint-host", help="Endpoint host name", hidden=True),
    ] = None,
) -> None:
    from ...common.progress import make_cli_progress
    from toolang.up import sandbox as sandbox_runtime

    selector = require_runtime_agent(ctx, agent)
    progress = make_cli_progress(preparation=True)
    launch: RuntimeLaunch | None = None
    launch_started = False
    try:
        with progress, ExitStack() as locks:
            target = cli_context(ctx).layout
            if target is None:
                parsed = user_call(agent_sources.parse_agent_target, selector)
                target = (
                    AgentLayout.resident(context_root(ctx), selector)
                    if parsed.form == "name"
                    else agent_sources.visiting_layout(
                        selector, toolang_root=context_root(ctx)
                    )
                )
            locks.enter_context(
                sandbox_runtime.management_lock(target, progress=progress.sink)
            )
            if _retain_started(
                ctx,
                target,
                sandbox=sandbox,
                driver=driver,
                dev=dev,
                model_catalog=model_catalog,
                compact_model=compact_model,
                allows=allows,
                defaults=defaults,
                limits=limits,
                port=port,
                endpoint_host=endpoint_host,
            ):
                return
            if target.placement == "visiting":
                target = agent_sources.resolve_visiting_layout(
                    selector,
                    toolang_root=context_root(ctx),
                    progress=progress.sink,
                )
            launch = resolve_startup(
                target,
                toolang_root=context_root(ctx),
                model_catalog=model_catalog,
                sandbox=sandbox,
                allows=allows,
                defaults=defaults,
                compact_model=compact_model,
                driver=driver,
                limits=limits,
                port=port,
                host=host,
                endpoint_host=endpoint_host,
                dev=dev,
                background=True,
            )
            with progress.suspended():
                warn_development_package_source(launch.startup)
            launch_started = True
            handle = user_call(
                asyncio.run,
                agents.AgentProcess(launch.target).start(
                    launch.startup,
                    progress=progress.sink,
                ),
            )
    except KeyboardInterrupt:
        raise typer.Exit(130) from None
    except (
        TimeoutError,
        RuntimeError,
        OSError,
        ValueError,
        ClickException,
    ) as exc:
        if isinstance(exc, ClickException) and not launch_started:
            raise
        if launch is None:
            raise ClickException(str(exc)) from exc
        raise ClickException(
            runtime_startup_failure_message(
                progress,
                exc,
                log_path=launch.target.runtime_log,
                dev_artifact=launch.startup.dev_artifact,
                development_build=development_source()[0],
            )
        ) from exc
    typer.echo(
        f"Agent {launch.target.name} started: {handle.state.ref.endpoint} "
        f"drivers={format_drivers(launch.startup.serve.drivers)} lifetime=persistent"
    )


def _retain_started(
    ctx: typer.Context,
    layout: AgentLayout,
    *,
    sandbox: str | None,
    driver: str | None,
    dev: Path | None,
    model_catalog: Path | None,
    compact_model: str | None,
    allows: list[str] | None,
    defaults: list[str] | None,
    limits: list[str] | None,
    port: int | None,
    endpoint_host: str | None,
) -> bool:
    from toolang.up import sandbox as sandbox_runtime

    with sandbox_runtime.management_lock(layout):
        process = agents.AgentProcess(layout)
        status = process.status(ui_base_url=ui_base_url())
        if status is not None and status.status in {"preparing", "starting"}:
            status = process.wait_ready(status, ui_base_url=ui_base_url(), timeout=30)
        if status is None or status.status != "running":
            return False
        if (
            allows
            or defaults
            or limits
            or endpoint_host is not None
            or ctx.get_parameter_source("host") == ParameterSource.COMMANDLINE
        ):
            raise ClickException(
                "startup options cannot be verified on attachment; stop and restart the agent"
            )
        if port is not None and port != status.port:
            raise ClickException(
                "--port differs from the running agent; stop and restart it"
            )
        try:
            with acquire_agent_server(
                layout,
                sandbox=sandbox,
                drivers=parse_drivers(driver) if driver is not None else None,
                dev=dev,
                model_catalog=model_catalog,
                compact_override=resolve_compact_override({}, compact_model),
                attach_only=True,
            ) as server:
                if server is None:
                    raise RuntimeError("agent acquisition requires a server")
                if server.launch_id is None:
                    raise ClickException(
                        "server lifetime is unmanaged; stop and restart it to use start"
                    )
                if server.lifetime == "foreground":
                    raise ClickException("start cannot detach foreground serve")
                if server.launch_id is not None:
                    with httpx.Client(timeout=3.0) as http:
                        response = http.post(
                            f"{server.endpoint}/api/v{AGENT_API_VERSION}/runtime/promote",
                            json={"launch_id": server.launch_id},
                        )
                        response.raise_for_status()
                actual = process.state() or {}
                raw_drivers = actual.get("drivers")
                running = (
                    parse_drivers(",".join(str(d) for d in raw_drivers) or "none")
                    if isinstance(raw_drivers, list)
                    else parse_drivers("none")
                )
                typer.echo(
                    f"Agent {layout.name} running: {server.endpoint} "
                    f"drivers={format_drivers(running)} lifetime=persistent"
                )
                return True
        except ServerClosingError:
            return False
        except (AgentServerAcquisitionError, httpx.HTTPError, ValueError) as exc:
            raise ClickException(str(exc)) from exc


def stop(
    ctx: typer.Context,
    agent: str | None = typer.Argument(
        None, help="Agent Name or remote Source Ref", hidden=True
    ),
    force: Annotated[
        bool,
        typer.Option(help="Force-stop when graceful shutdown does not complete"),
    ] = False,
) -> None:
    from ...common.progress import make_cli_progress

    agent_name = require_runtime_agent(ctx, agent)
    root = context_root(ctx)
    layout = cli_context(ctx).layout or AgentLayout.resident(root, agent_name)
    progress = make_cli_progress()
    try:
        with progress:
            stopped = user_call(
                asyncio.run,
                agents.AgentProcess(layout).stop(
                    force=force,
                    progress=progress.sink,
                ),
            )
    except (OSError, RuntimeError, ClickException) as exc:
        if progress.failure_stage is None:
            raise
        raise ClickException(
            progress.failure_message(exc, log_path=layout.runtime_log)
        ) from exc
    if not stopped:
        raise ClickException(f"Agent {agent_name} not running")
    typer.echo(f"Agent {agent_name} stopped")


def serve(
    ctx: typer.Context,
    agent: Annotated[
        str,
        typer.Argument(metavar="AGENT", click_type=TextType(), help="Local agent name"),
    ],
    allows: AllowOptions = None,
    limits: LimitOptions = None,
    defaults: DefaultOptions = None,
    compact_model: CompactModelOption = None,
    driver: DriverOption = None,
    model_catalog: ModelCatalogOption = None,
    host: Annotated[
        str, typer.Option("--host", metavar="HOST", help="API bind host")
    ] = "127.0.0.1",
    endpoint_host: Annotated[
        str | None,
        typer.Option(
            "--endpoint-host", metavar="HOST", help="Externally visible endpoint host"
        ),
    ] = None,
    port: Annotated[
        int, typer.Option("--port", metavar="PORT", help="API bind port")
    ] = 7002,
    log_spec: Annotated[
        str | None,
        typer.Option("--log", metavar="LOG_SPEC", help="Python logging specification"),
    ] = None,
    agent_root: Annotated[
        Path | None, typer.Option("--agent-root", hidden=True)
    ] = None,
    source_policy: Annotated[
        str, typer.Option("--source-policy", hidden=True)
    ] = "root",
    membership: Annotated[str, typer.Option("--membership", hidden=True)] = "managed",
    sandbox: Annotated[str, typer.Option("--sandbox", hidden=True)] = "host",
    location: Annotated[str, typer.Option("--location", hidden=True)] = "host",
    launch_id: Annotated[str | None, typer.Option("--launch-id", hidden=True)] = None,
    lifetime: Annotated[
        ServerLifetime, typer.Option("--lifetime", hidden=True)
    ] = "persistent",
    runtime_instance: Annotated[
        str | None, typer.Option("--runtime-instance", hidden=True)
    ] = None,
    workspace_bindings: Annotated[
        str, typer.Option("--workspace-bindings", hidden=True)
    ] = "{}",
    initial_workdir: Annotated[
        str | None, typer.Option("--initial-workdir", hidden=True)
    ] = None,
) -> None:
    """Run the internal AgentServer entrypoint."""

    from toolang.up.server import resolve_serve, serve as serve_agent
    from toolang.up.config import parse_drivers
    from toolang.up.types import ALL_DRIVERS
    from toolang.plugin.sandboxes.host import HOST_LAUNCH_ENV

    launch_id = launch_id or os.environ.pop(HOST_LAUNCH_ENV, None)
    if (
        source_policy not in {"project", "root"}
        or membership not in {"managed", "unmanaged"}
        or location not in {"host", "guest"}
        or lifetime not in {"persistent", "foreground", "automatic"}
    ):
        raise ClickException("invalid workload startup inputs")
    layout = AgentLayout(
        agent_root or context_root(ctx),
        agent,
        "resident",
        instance=runtime_instance,
        project_inputs=source_policy == "project",
        managed_membership=membership == "managed",
    )
    environ = load_runtime_environ(layout, base_environ=os.environ)
    environ["TOOLANG_ROOT"] = str(context_root(ctx))
    if model_catalog := resolve_model_catalog_option(model_catalog):
        environ[MODEL_CATALOG_ENV] = str(model_catalog)
    spec = user_call(
        resolve_serve,
        layout=layout,
        toolang_root=context_root(ctx),
        launch_id=launch_id,
        lifetime=lifetime,
        drivers=user_call(parse_drivers, driver) if driver is not None else ALL_DRIVERS,
        host=host,
        endpoint_host=endpoint_host,
        port=port,
        ceiling_overrides=user_call(resolve_ceiling_overrides, {}, allows),
        default_overrides=user_call(resolve_default_overrides, {}, defaults),
        compact_override=user_call(resolve_compact_override, environ, compact_model),
        limit_overrides=user_call(resolve_limit_overrides, {}, limits),
        log_spec=log_spec,
        workspace_additions=json.loads(workspace_bindings),
        workdir=initial_workdir,
    )
    raise typer.Exit(
        user_call(
            serve_agent,
            spec,
            environ=environ,
            sandbox=sandbox,
            launch_id=launch_id,
            location=location,
        )
    )


def resolve_startup(
    target: AgentLayout,
    *,
    model_catalog: Path | None,
    toolang_root: Path | None = None,
    sandbox: str | None,
    allows: list[str] | None,
    defaults: list[str] | None,
    limits: list[str] | None,
    port: int | None,
    host: str,
    endpoint_host: str | None,
    dev: Path | None,
    background: bool,
    compact_model: str | None = None,
    driver: str | None = None,
    workspace_options: list[str] | None = None,
    workdir: str | None = None,
) -> RuntimeLaunch:
    from toolang.up import sandbox as sandbox_runtime
    from toolang.cli.common.ports import agent_port

    from toolang.up.config import default_drivers, parse_drivers

    drivers = (
        user_call(parse_drivers, driver)
        if driver is not None
        else default_drivers(target.placement)
    )
    root, agent = target.root, target.name
    workspaces = resolve_workspaces(
        target, procdir=Path.cwd(), paths=workspace_options or (), workdir=workdir
    )
    if target.placement == "resident" and not target.home.is_dir():
        raise ClickException(f"Agent {agent} not found")
    existing = agents.AgentProcess(target).status(ui_base_url=ui_base_url())
    if existing is not None and existing.status in {"running", "preparing", "starting"}:
        raise ClickException(active_agent_error(existing))
    environ = load_runtime_environ(target, base_environ=os.environ)
    toolang_root = (
        toolang_root
        or target.toolang_root
        or Path(environ.get("TOOLANG_ROOT", str(root)))
    )
    environ["TOOLANG_ROOT"] = str(toolang_root)
    port = user_call(agent_port, target, port, environ=environ)
    if model_catalog := resolve_model_catalog_option(model_catalog):
        environ[MODEL_CATALOG_ENV] = str(model_catalog)
    log_plan = resolve_agent_logging(
        mode="start" if background else "run",
        environ=environ,
        agent_log_path=target.runtime_log,
    )
    if not background:
        configure_logging_plan(log_plan)
    ceiling_overrides = user_call(
        resolve_ceiling_overrides,
        log_plan.environ,
        allows,
    )
    default_overrides = user_call(
        resolve_default_overrides,
        log_plan.environ,
        defaults,
    )
    limit_overrides = user_call(
        resolve_limit_overrides,
        log_plan.environ,
        limits,
    )
    startup = user_call(
        asyncio.run,
        sandbox_runtime.resolve_launch(
            layout=target,
            drivers=drivers,
            lifetime="persistent" if background else "foreground",
            host=host,
            endpoint_host=endpoint_host,
            port=port,
            sandbox=sandbox,
            ceiling_overrides=ceiling_overrides,
            default_overrides=default_overrides,
            compact_override=user_call(
                resolve_compact_override, log_plan.environ, compact_model
            ),
            limit_overrides=limit_overrides,
            dev=dev,
            log_spec=log_plan.spec,
            output="file" if background else "inherit",
            log_path=log_plan.path,
            toolang_root=toolang_root,
            environ=log_plan.environ,
            workspace_additions=workspaces.additions,
            workdir=workspaces.workdir,
        ),
    )
    return RuntimeLaunch(target, startup, log_plan.environ, log_plan)
