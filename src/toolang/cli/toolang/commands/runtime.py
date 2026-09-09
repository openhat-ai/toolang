"""Foreground and background AgentServer commands."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import os
from pathlib import Path
from typing import Annotated, TYPE_CHECKING

import typer
from typer._click.exceptions import ClickException

from toolang.cli.common.parameters import (
    AllowOptions,
    CompactModelOption,
    DefaultOptions,
    LimitOptions,
    TextType,
)

from toolang.common.layout import AgentLayout
from toolang.plugin.models.catalog import MODEL_CATALOG_ENV
from toolang.cli.common.policy import (
    resolve_default_overrides,
    resolve_compact_override,
    resolve_ceiling_overrides,
    resolve_limit_overrides,
)
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
    from toolang.up.sandbox import SandboxState, LaunchSpec

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
        help="Agent name, .too file, reference, or URL",
        hidden=True,
    ),
    sandbox: Annotated[
        str | None,
        typer.Option(
            "--sandbox",
            metavar="SANDBOX_SPEC",
            help="Run in this sandbox; defaults to agent config or host",
        ),
    ] = None,
    allows: AllowOptions = None,
    limits: LimitOptions = None,
    defaults: DefaultOptions = None,
    compact_model: CompactModelOption = None,
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
    from toolang.up import sandbox as sandbox_runtime
    from ...common.progress import make_cli_progress

    selector = require_runtime_agent(ctx, agent)
    progress = make_cli_progress()
    cleanup_progress = make_cli_progress(leading_gap=True)
    launch: RuntimeLaunch | None = None
    launch_started = False
    try:
        with progress, cleanup_progress:
            selected_layout = cli_context(ctx).layout
            target = (
                selected_layout
                if selected_layout is not None
                else agents.resolve_run_layout(
                    context_root(ctx),
                    selector,
                    progress=progress.sink,
                )
            )
            launch = resolve_startup(
                target,
                model_catalog=model_catalog,
                sandbox=sandbox,
                allows=allows,
                defaults=defaults,
                compact_model=compact_model,
                limits=limits,
                port=port,
                host=host,
                endpoint_host=endpoint_host,
                dev=dev,
                background=False,
            )
            with progress.suspended():
                warn_development_package_source(launch.startup)
            launch_started = True
            exit_code = user_call(
                asyncio.run,
                sandbox_runtime.run(
                    launch.startup,
                    on_ready=lambda state: _report_foreground_ready(
                        launch.target.name,
                        state,
                        progress,
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
    agent: str | None = typer.Argument(None, help="Local agent name", hidden=True),
    sandbox: Annotated[
        str | None,
        typer.Option(
            "--sandbox",
            metavar="SANDBOX_SPEC",
            help="Run in this sandbox; defaults to agent config or host",
        ),
    ] = None,
    allows: AllowOptions = None,
    limits: LimitOptions = None,
    defaults: DefaultOptions = None,
    compact_model: CompactModelOption = None,
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
    from toolang.up import sandbox as sandbox_runtime
    from ...common.progress import make_cli_progress

    selector = require_runtime_agent(ctx, agent)
    if user_call(agents.parse_agent_selector, selector).form != "name":
        raise ClickException(
            "start only supports local agent names; clone the remote source first"
        )
    progress = make_cli_progress()
    launch: RuntimeLaunch | None = None
    launch_started = False
    try:
        with progress:
            target = agents.resolve_run_layout(
                context_root(ctx),
                selector,
                progress=progress.sink,
            )
            launch = resolve_startup(
                target,
                model_catalog=model_catalog,
                sandbox=sandbox,
                allows=allows,
                defaults=defaults,
                compact_model=compact_model,
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
                sandbox_runtime.launch(
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
    typer.echo(f"Agent {launch.target.name} started: {handle.state.ref.endpoint}")


def stop(
    ctx: typer.Context,
    agent: str | None = typer.Argument(None, help="Local agent name", hidden=True),
    force: Annotated[
        bool,
        typer.Option(help="Force-stop when graceful shutdown does not complete"),
    ] = False,
) -> None:
    from toolang.up import sandbox as sandbox_runtime
    from ...common.progress import make_cli_progress

    agent_name = require_runtime_agent(ctx, agent)
    root = context_root(ctx)
    layout = AgentLayout.resident(root, agent_name)
    progress = make_cli_progress()
    try:
        with progress:
            stopped = user_call(
                asyncio.run,
                sandbox_runtime.stop(
                    layout,
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
    ] = 7001,
    log_spec: Annotated[
        str | None,
        typer.Option("--log", metavar="LOG_SPEC", help="Python logging specification"),
    ] = None,
) -> None:
    """Run the internal AgentServer entrypoint."""

    from toolang.up.server import resolve_serve, serve as serve_agent

    layout = AgentLayout.resident(context_root(ctx), agent)
    sandbox = os.environ.get("TOOLANG_SANDBOX", "host").strip() or "host"
    environ = load_runtime_environ(layout, base_environ=os.environ)
    environ["TOOLANG_ROOT"] = str(layout.root)
    if model_catalog := resolve_model_catalog_option(model_catalog):
        environ[MODEL_CATALOG_ENV] = str(model_catalog)
    spec = user_call(
        resolve_serve,
        layout=layout,
        host=host,
        endpoint_host=endpoint_host,
        port=port,
        ceiling_overrides=user_call(resolve_ceiling_overrides, {}, allows),
        default_overrides=user_call(resolve_default_overrides, {}, defaults),
        compact_override=user_call(resolve_compact_override, environ, compact_model),
        limit_overrides=user_call(resolve_limit_overrides, {}, limits),
        log_spec=log_spec,
    )
    raise typer.Exit(
        user_call(
            serve_agent,
            spec,
            environ=environ,
            sandbox=sandbox,
        )
    )


def resolve_startup(
    target: AgentLayout,
    *,
    model_catalog: Path | None,
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
) -> RuntimeLaunch:
    from toolang.up import sandbox as sandbox_runtime

    root, agent = target.root, target.name
    if target.placement == "resident" and not target.home.is_dir():
        raise ClickException(f"Agent {agent} not found")
    existing = agents.AgentProcess(target).status(ui_base_url=ui_base_url())
    if existing is not None and existing.status in {"running", "preparing", "starting"}:
        raise ClickException(active_agent_error(existing))
    environ = load_runtime_environ(target, base_environ=os.environ)
    environ["TOOLANG_ROOT"] = str(root)
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
            temporary_port=target.placement == "visiting" and port is None,
            environ=log_plan.environ,
        ),
    )
    return RuntimeLaunch(target, startup, log_plan.environ, log_plan)
