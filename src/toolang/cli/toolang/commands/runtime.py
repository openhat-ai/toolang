"""Foreground and background AgentServer commands."""

from __future__ import annotations

import asyncio
from contextlib import ExitStack
from dataclasses import replace
import os
import sys
import json
from pathlib import Path
from typing import Annotated, TYPE_CHECKING
from toolang.common.config_runtime import LAUNCH_SCHEMA
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

from ...common.execution import select_agent_instance
from toolang.cli.common.agent_argument import AgentArgument, AgentNameType
from typer._click.core import ParameterSource
from typer._click.exceptions import ClickException

from toolang.cli.common.parameters import (
    AllowOptions,
    CompactModelOption,
    DriverOption,
    DefaultOptions,
    LimitOptions,
)

from toolang.common.layout import AgentLayout
from toolang.common.environment import (
    EnvironmentPolicy,
    RUNTIME_ENV_NAMES,
    ResolvedEnvironment,
    environment_identity,
    environment_scope,
)
from toolang.up.types import ServerLifetime
from toolang.up.config import parse_drivers
from toolang.cli.common.agent_server import (
    acquire_agent_server,
    AgentServerAcquisitionError,
    ServerClosingError,
)

import httpx

from toolang.base.errors import ToolangError
from toolang.plugin.catalogs.models_dev.path import MODEL_CATALOG_ENV
from toolang.cli.common.policy import (
    resolve_default_overrides,
    resolve_compact_override,
    resolve_ceiling_overrides,
    resolve_limit_overrides,
)
from toolang.up import source as agent_sources
from toolang.up import process as agents
from ...common.startup import resolve_attachment_requirements, resolve_runtime_launch
from ...common.context import (
    ModelCatalogOption,
    cli_context,
    context_layout,
    context_root,
    require_runtime_agent,
    load_runtime_environment,
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


def run(
    ctx: typer.Context,
    agent: Annotated[
        str,
        AgentArgument("execute", placements="resident,visiting", both_orders=True),
    ],
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
    if cli_context(ctx).address is not None:
        context_layout(ctx)
    progress = make_cli_progress(preparation=True)
    cleanup_progress = make_cli_progress(leading_gap=True)
    launch: LaunchSpec | None = None
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
            prepared = (
                target
                if cli_context(ctx).instance_selected
                else agent_sources.prepare_visiting_layout(
                    target,
                    toolang_root=context_root(ctx),
                    progress=progress.sink,
                )
            )
            if prepared is not target:
                launch = replace(launch, serve=replace(launch.serve, layout=prepared))
            with progress.suspended():
                warn_development_package_source(launch)
            launch_started = True
            if launch.sandbox == "host":
                from toolang.up.server import serve as serve_agent
                from toolang.plugin.sandboxes.host import (
                    host_sandbox_description,
                    HOST_SANDBOX_DESCRIPTION_ENV,
                )

                environ = dict(launch.environ)
                environ[HOST_SANDBOX_DESCRIPTION_ENV] = host_sandbox_description()
                user_call(
                    sandbox_runtime.prepare_launch_state,
                    launch,
                    progress=progress.sink,
                )
                exit_code = user_call(
                    serve_agent,
                    replace(launch.serve, toolang_root=context_root(ctx)),
                    environ=environ,
                    sandbox="host",
                )
            else:
                from ...common.foreground import run_foreground
                from toolang.up.invocation import ForegroundServerOwner

                with ForegroundServerOwner(launch.serve.layout) as owner:

                    def ready(state: SandboxState) -> None:
                        owner.bind(state.launch_id)
                        assert launch is not None
                        _report_foreground_ready(
                            launch.serve.layout.name, state, progress
                        )

                    exit_code = user_call(
                        run_foreground,
                        sandbox_runtime.run(
                            launch,
                            on_ready=ready,
                            progress=progress.sink,
                            cleanup_progress=cleanup_progress.sink,
                            closing=owner.closing,
                        ),
                        closing=owner.closing,
                        interrupt_reason="server interrupted",
                    )
    except KeyboardInterrupt:
        raise typer.Exit(130) from None
    except (
        FileExistsError,
        FileNotFoundError,
        OSError,
        RuntimeError,
        ToolangError,
        ValueError,
        ClickException,
    ) as exc:
        if not launch_started and progress.failure_stage is not None:
            raise ClickException(progress.failure_message(exc)) from exc
        if isinstance(exc, ClickException) and not launch_started:
            raise
        if launch_started and launch is not None:
            if cleanup_progress.failure_stage is not None:
                message = cleanup_progress.failure_message(
                    exc,
                    log_path=launch.serve.layout.runtime_log,
                )
            else:
                message = runtime_startup_failure_message(
                    progress,
                    exc,
                    dev_artifact=launch.dev_artifact,
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
    agent: Annotated[
        str,
        AgentArgument("execute", placements="resident,visiting", both_orders=True),
    ],
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
    if cli_context(ctx).address is not None:
        context_layout(ctx)
    progress = make_cli_progress(preparation=True)
    launch: LaunchSpec | None = None
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
                host=(
                    host
                    if ctx.get_parameter_source("host") == ParameterSource.COMMANDLINE
                    else None
                ),
            ):
                return
            target = (
                target
                if cli_context(ctx).instance_selected
                else agent_sources.prepare_visiting_layout(
                    target,
                    toolang_root=context_root(ctx),
                    source_ref=selector,
                    progress=progress.sink,
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
                background=True,
            )
            with progress.suspended():
                warn_development_package_source(launch)
            launch_started = True
            handle = user_call(
                asyncio.run,
                agents.AgentProcess(launch.serve.layout).start(
                    launch,
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
                log_path=launch.serve.layout.runtime_log,
                dev_artifact=launch.dev_artifact,
                development_build=development_source()[0],
            )
        ) from exc
    typer.echo(f"Agent {launch.serve.layout.name} started: {handle.state.ref.endpoint}")


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
    host: str | None = None,
) -> bool:
    from toolang.up import sandbox as sandbox_runtime

    with sandbox_runtime.management_lock(layout):
        process = agents.AgentProcess(layout)
        status = process.status(ui_base_url=ui_base_url())
        if status is not None and status.status in {"preparing", "starting"}:
            status = process.wait_ready(status, ui_base_url=ui_base_url(), timeout=30)
        if status is None or status.status != "running":
            return False
        requested = resolve_attachment_requirements(
            sandbox=status.sandbox or "host",
            allows=allows,
            defaults=defaults,
            limits=limits,
            port=port,
            host=host,
            endpoint_host=endpoint_host,
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
                attachment_requirements=requested,
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
                typer.echo(f"Agent {layout.name} running: {server.endpoint}")
                return True
        except ServerClosingError:
            return False
        except (AgentServerAcquisitionError, httpx.HTTPError, ValueError) as exc:
            raise ClickException(str(exc)) from exc


def stop(
    ctx: typer.Context,
    agent: Annotated[
        str,
        AgentArgument("home", placements="resident,visiting", both_orders=True),
    ],
    force: Annotated[
        bool,
        typer.Option(help="Force-stop when graceful shutdown does not complete"),
    ] = False,
) -> None:
    from ...common.progress import make_cli_progress

    agent_name = require_runtime_agent(ctx, agent)
    layout = select_agent_instance(ctx)
    from toolang.up.instance import read_instance

    entry = read_instance(layout.runtime_status)
    expected_launch_id = cli_context(ctx).launch_id or (
        entry["launch_id"] if entry else None
    )
    progress = make_cli_progress()
    try:
        with progress:
            stopped = user_call(
                asyncio.run,
                agents.AgentProcess(layout).stop(
                    force=force,
                    progress=progress.sink,
                    **(
                        {"expected_launch_id": expected_launch_id}
                        if expected_launch_id is not None
                        else {}
                    ),
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
        typer.Argument(metavar="NAME", click_type=AgentNameType(), help="Agent name"),
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
    cors_allowed_origins: Annotated[
        str | None, typer.Option("--cors-allowed-origins", hidden=True)
    ] = None,
    ui_base_url: Annotated[
        str | None, typer.Option("--ui-base-url", hidden=True)
    ] = None,
    place: Annotated[Path | None, typer.Option("--place", hidden=True)] = None,
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
    place_code: Annotated[str | None, typer.Option("--place-code", hidden=True)] = None,
    placement: Annotated[str, typer.Option("--placement", hidden=True)] = "resident",
    source_path: Annotated[
        Path | None, typer.Option("--source-path", hidden=True)
    ] = None,
    source_ref: Annotated[str | None, typer.Option("--source-ref", hidden=True)] = None,
    store_schema: Annotated[int, typer.Option("--store-schema", hidden=True)] = 63,
    launch_schema: Annotated[
        int, typer.Option("--launch-schema", hidden=True)
    ] = LAUNCH_SCHEMA,
    source_edit_lock: Annotated[
        Path | None, typer.Option("--source-edit-lock", hidden=True)
    ] = None,
    registry_directory: Annotated[
        Path | None, typer.Option("--registry-directory", hidden=True)
    ] = None,
    registry_status: Annotated[
        Path | None, typer.Option("--registry-status", hidden=True)
    ] = None,
    workspace_bindings: Annotated[
        str, typer.Option("--workspace-bindings", hidden=True)
    ] = "{}",
    environment_file: Annotated[
        Path | None, typer.Option("--environment-file", hidden=True)
    ] = None,
    initial_workdir: Annotated[
        str | None, typer.Option("--initial-workdir", hidden=True)
    ] = None,
) -> None:
    """Run the internal AgentServer entrypoint."""

    from toolang.up.server import resolve_serve, serve as serve_agent
    from toolang.up.config import parse_drivers
    from toolang.up.types import ALL_DRIVERS
    from toolang.plugin.sandboxes.host import HOST_LAUNCH_ENV

    host_launch = os.environ.pop(HOST_LAUNCH_ENV, None)
    launch_id = launch_id or host_launch
    if (
        source_policy not in {"project", "root"}
        or membership not in {"managed", "unmanaged"}
        or location not in {"host", "guest"}
        or lifetime not in {"persistent", "foreground", "automatic"}
    ):
        raise ClickException("invalid workload startup inputs")
    from toolang.execution.store import RUN_STORE_SCHEMA
    from typing import cast
    from toolang.common.layout import AgentPlacement

    if (
        store_schema != RUN_STORE_SCHEMA
        or launch_schema != LAUNCH_SCHEMA
        or placement
        not in {
            "resident",
            "visiting",
            "roaming",
        }
    ):
        raise ClickException(
            "workload requires a compatible Toolang runtime/store schema"
        )
    if runtime_instance is not None:
        raise ClickException("legacy runtime instances cannot be resumed")
    layout = AgentLayout(
        place or context_root(ctx),
        agent,
        cast(AgentPlacement, placement),
        root=context_root(ctx),
        place_code=place_code,
        source_path=source_path,
        source_ref=source_ref,
        source_edit_lock=source_edit_lock,
        project_inputs=source_policy == "project",
        managed_membership=membership == "managed",
    )
    environ = dict(os.environ)
    environ["TOOLANG_ROOT"] = str(context_root(ctx))
    environ["TOOLANG_SANDBOX"] = sandbox
    if environment_file is not None:
        from toolang.common.environment import apply_environment_file

        if location != "guest":
            raise ClickException("an explicit workload environment requires a guest")
        grants = user_call(apply_environment_file, environment_file, environ)
        selected = ResolvedEnvironment(
            context_root(ctx),
            EnvironmentPolicy(),
            environ,
            identity=environment_identity(layout),
            source=environment_file,
            guest_values=grants,
        )
    elif location == "host" and host_launch:
        # The controller already resolved and reduced this exec environment.
        selected = ResolvedEnvironment(
            context_root(ctx),
            EnvironmentPolicy(),
            environ,
            identity=environment_identity(layout),
        )
    else:
        # Guests load their generated root file; standalone hosts apply policy.
        selected = user_call(load_runtime_environment, layout, base_environ=environ)
    from toolang.common.config_runtime import (
        CONFIG_PATCH_ENV,
        RuntimeConfig,
        parse_workspace_bindings,
    )

    bindings = user_call(parse_workspace_bindings, workspace_bindings)
    if location == "host" and any(
        binding.source != binding.path for binding in bindings.values()
    ):
        raise ClickException("host workspace bindings must preserve source paths")
    controls = {
        name: value for name, value in environ.items() if name in RUNTIME_ENV_NAMES
    }
    environ = dict(selected.values) if location == "host" else environ
    environ.update(selected.guest_values)
    for name in RUNTIME_ENV_NAMES:
        environ.pop(name, None)
    environ.update(controls)
    if location == "guest":
        bin_dir = str(Path(sys.argv[0]).absolute().parent)
        path = environ.get("PATH", "")
        environ["PATH"] = os.pathsep.join(
            [bin_dir, *(part for part in path.split(os.pathsep) if part != bin_dir)]
            if path
            else [bin_dir]
        )
    if model_catalog := resolve_model_catalog_option(model_catalog):
        environ[MODEL_CATALOG_ENV] = str(model_catalog)
    layout = replace(
        layout,
        runtime_config=user_call(
            RuntimeConfig.parse, environ.get(CONFIG_PATCH_ENV), workspaces=bindings
        ),
    )
    environment = replace(selected, values=environ)
    with environment_scope(environment):
        cors_origins = None
        if cors_allowed_origins is not None:
            parsed = user_call(json.loads, cors_allowed_origins)
            if not isinstance(parsed, list) or not all(
                isinstance(item, str) for item in parsed
            ):
                raise ClickException("invalid CORS origins startup input")
            cors_origins = tuple(parsed)
        spec = user_call(
            resolve_serve,
            layout=layout,
            toolang_root=context_root(ctx),
            launch_id=launch_id,
            lifetime=lifetime,
            drivers=user_call(parse_drivers, driver)
            if driver is not None
            else ALL_DRIVERS,
            host=host,
            endpoint_host=endpoint_host,
            port=port,
            ceiling_overrides=user_call(resolve_ceiling_overrides, {}, allows),
            default_overrides=user_call(resolve_default_overrides, {}, defaults),
            compact_override=user_call(
                resolve_compact_override, environ, compact_model
            ),
            limit_overrides=user_call(resolve_limit_overrides, {}, limits),
            log_spec=log_spec,
            cors_allowed_origins=cors_origins,
            ui_base_url=ui_base_url,
            workspace_additions={
                name: str(binding.source)
                for name, binding in bindings.items()
                if binding.temporary
            },
            workdir=initial_workdir,
        )
        spec = replace(
            spec, registry_directory=registry_directory, registry_status=registry_status
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
) -> LaunchSpec:
    from toolang.up.config import default_drivers, parse_drivers

    if driver is not None:
        drivers = user_call(parse_drivers, driver)
    else:
        drivers = default_drivers(target.placement)
    place, agent = target.place, target.name
    workspaces = resolve_workspaces(
        target, procdir=Path.cwd(), paths=workspace_options or (), workdir=workdir
    )
    if target.placement == "resident" and not target.home.is_dir():
        raise ClickException(f"Agent {agent} not found")
    existing = agents.AgentProcess(target).status(ui_base_url=ui_base_url())
    if existing is not None and existing.status in {"running", "preparing", "starting"}:
        raise ClickException(active_agent_error(existing))
    environment = load_runtime_environment(target, base_environ=os.environ)
    environ = dict(environment.values)
    toolang_root = (
        toolang_root or target.root or Path(environ.get("TOOLANG_ROOT", str(place)))
    )
    return user_call(
        resolve_runtime_launch,
        target,
        toolang_root=toolang_root,
        environ=environ,
        guest_environ=environment.guest_values,
        drivers=drivers,
        lifetime="persistent" if background else "foreground",
        background=background,
        sandbox=sandbox,
        dev=dev,
        model_catalog=resolve_model_catalog_option(model_catalog),
        host=host,
        endpoint_host=endpoint_host,
        port=port,
        allows=allows,
        defaults=defaults,
        limits=limits,
        compact_override=user_call(resolve_compact_override, {}, compact_model),
        workspace_additions=workspaces.additions,
        workdir=workspaces.workdir,
    )
