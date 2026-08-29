"""Foreground and background agent runtime commands."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import os
from pathlib import Path
from typing import Annotated, TYPE_CHECKING

import click
import typer

from toolang.common.layout import AgentLayout
from toolang.plugin.models.catalog import MODEL_CATALOG_ENV
from toolang.cli.common.policy import (
    resolve_binding_overrides,
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
    cli_context,
    command_progress,
    context_model_catalog,
    context_root,
    require_runtime_agent,
    load_runtime_environ,
    ui_base_url,
    user_call,
)
from ...common.execution_runtime import (
    DEVELOPMENT_WHEEL_HELP,
    warn_development_package_source,
)
from ...common.output import active_agent_error, echo_error
from toolang.common.version import development_source

if TYPE_CHECKING:
    from toolang.common.progress import ProgressSink
    from toolang.up.sandbox import SandboxState, LaunchSpec
    from ...common.progress import CliProgress

from ...common.progress import runtime_startup_failure_message


@dataclass(frozen=True, slots=True)
class RuntimeLaunch:
    target: AgentLayout
    startup: LaunchSpec
    environ: dict[str, str]
    log_plan: LoggingPlan


@dataclass(frozen=True, slots=True)
class _RoamingFileOptions:
    inboxes: tuple[Path, ...]
    allows: tuple[str, ...]
    defaults: tuple[str, ...]
    limits: tuple[str, ...]
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
    from toolang.up import sandbox as sandbox_runtime
    from ...common.context import load_runtime_environ
    from ...common.progress import make_cli_progress

    startup_progress: CliProgress | None = None
    startup: LaunchSpec | None = None
    try:
        options = _parse_roaming_file_options(args)
        startup_progress = make_cli_progress()
        layout = agents.materialize_roaming_program(
            source,
            progress=startup_progress.sink,
        )
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
        ceiling_overrides = user_call(
            resolve_ceiling_overrides,
            log_plan.environ,
            options.allows,
        )
        binding_overrides = user_call(
            resolve_binding_overrides,
            log_plan.environ,
            options.defaults,
        )
        limit_overrides = user_call(
            resolve_limit_overrides,
            log_plan.environ,
            options.limits,
        )
        startup = user_call(
            asyncio.run,
            sandbox_runtime.resolve_launch(
                layout=layout,
                host=options.host,
                endpoint_host=options.endpoint_host,
                port=options.port,
                sandbox=options.sandbox,
                ceiling_overrides=ceiling_overrides,
                binding_overrides=binding_overrides,
                limit_overrides=limit_overrides,
                file_inboxes=options.inboxes,
                dev=options.dev,
                log_spec=log_plan.spec,
                output="file",
                log_path=layout.runtime_log,
                temporary_port=options.port is None,
                environ=log_plan.environ,
                progress=startup_progress.sink,
            ),
        )
        warn_development_package_source(startup, progress=startup_progress)
        exit_code = user_call(
            asyncio.run,
            _run_foreground(
                startup,
                name=layout.name,
                progress=startup_progress,
            ),
        )
        return exit_code
    except KeyboardInterrupt:
        if startup_progress is not None:
            startup_progress.close()
        return 130
    except (
        FileExistsError,
        FileNotFoundError,
        OSError,
        RuntimeError,
        ValueError,
        click.ClickException,
    ) as exc:
        if startup_progress is not None:
            startup_progress.close()
        if startup_progress is not None:
            message = runtime_startup_failure_message(
                startup_progress,
                exc,
                dev_artifact=startup.dev_artifact if startup is not None else None,
                development_build=development_source()[0],
            )
        else:
            message = exc.message if isinstance(exc, click.ClickException) else str(exc)
        echo_error(message)
        return 1


def _parse_roaming_file_options(argv: list[str]) -> _RoamingFileOptions:
    inboxes: list[Path] = []
    allows: list[str] = []
    defaults: list[str] = []
    limits: list[str] = []
    host = "127.0.0.1"
    endpoint_host: str | None = None
    port: int | None = None
    sandbox = "host"
    dev: Path | None = None
    index = 0
    while index < len(argv):
        token = argv[index]
        option, separator, inline = token.partition("=")
        if option in {
            "--inbox",
            "--allow",
            "--default",
            "--limit",
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
            elif option == "--allow":
                allows.append(value)
            elif option == "--default":
                defaults.append(value)
            elif option == "--limit":
                limits.append(value)
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
        allows=tuple(allows),
        defaults=tuple(defaults),
        limits=tuple(limits),
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
        typer.Option(help="Run in this sandbox; defaults to agent config or host."),
    ] = None,
    allows: Annotated[
        list[str] | None,
        typer.Option("--allow", help="Set DOMAIN=SELECTORS. Repeat by domain."),
    ] = None,
    limits: Annotated[
        list[str] | None,
        typer.Option(
            "--limit",
            help="Set FIELD=VALUE. Repeat for another field.",
        ),
    ] = None,
    defaults: Annotated[
        list[str] | None,
        typer.Option("--default", help="Set FIELD=VALUE. Repeat for another field."),
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
            help=DEVELOPMENT_WHEEL_HELP,
        ),
    ] = None,
    endpoint_host: Annotated[
        str | None,
        typer.Option("--endpoint-host", help="Endpoint host name.", hidden=True),
    ] = None,
) -> None:
    selector = require_runtime_agent(ctx, agent)
    progress = command_progress(ctx)
    launch: RuntimeLaunch | None = None
    try:
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
            ctx,
            target,
            sandbox=sandbox,
            allows=allows,
            defaults=defaults,
            limits=limits,
            inboxes=inboxes,
            port=port,
            host=host,
            endpoint_host=endpoint_host,
            dev=dev,
            background=False,
            progress=progress.sink,
        )
        warn_development_package_source(launch.startup, progress=progress)
        exit_code = user_call(
            asyncio.run,
            _run_foreground(
                launch.startup,
                name=launch.target.name,
                progress=progress,
            ),
        )
    except KeyboardInterrupt:
        progress.close()
        raise typer.Exit(130) from None
    except (
        FileExistsError,
        FileNotFoundError,
        OSError,
        RuntimeError,
        ValueError,
        click.ClickException,
    ) as exc:
        progress.close()
        operational_failed = launch is not None and progress.failure_stage is not None
        if isinstance(exc, click.ClickException) and not operational_failed:
            raise
        if operational_failed and launch is not None:
            raise click.ClickException(
                runtime_startup_failure_message(
                    progress,
                    exc,
                    dev_artifact=launch.startup.dev_artifact,
                    development_build=development_source()[0],
                )
            ) from exc
        raise click.ClickException(str(exc)) from exc
    raise typer.Exit(exit_code)


def _report_foreground_ready(
    name: str,
    state: SandboxState,
) -> None:
    typer.echo(
        f"Agent {name} running: {state.ref.endpoint} (Ctrl+C to stop)",
        err=True,
    )


async def _run_foreground(
    startup: LaunchSpec,
    *,
    name: str,
    progress: CliProgress,
) -> int:
    """Run one foreground sandbox with explicit terminal-owner handoffs."""

    from toolang.up import sandbox as sandbox_runtime
    from ...common.progress import make_cli_progress

    handle = await sandbox_runtime.launch(startup, progress=progress.sink)
    progress.close()
    _report_foreground_ready(name, handle.state)
    try:
        if handle.plan is not None:
            await handle.implementation.follow(handle.plan, handle.state.ref)
        exit_code = await handle.implementation.wait(handle.state.ref)
    except asyncio.CancelledError:
        with make_cli_progress() as cleanup:
            await asyncio.shield(
                sandbox_runtime.stop_handle(
                    startup.serve.layout,
                    handle,
                    force=False,
                    progress=cleanup.sink,
                )
            )
        raise
    except BaseException:
        with make_cli_progress() as cleanup:
            await asyncio.shield(
                sandbox_runtime.stop_handle(
                    startup.serve.layout,
                    handle,
                    force=True,
                    progress=cleanup.sink,
                )
            )
        raise
    with make_cli_progress() as cleanup:
        await sandbox_runtime.release_handle(
            startup.serve.layout,
            handle,
            progress=cleanup.sink,
        )
    return exit_code


def start(
    ctx: typer.Context,
    agent: str | None = typer.Argument(
        None, help="Existing local agent name.", hidden=True
    ),
    sandbox: Annotated[
        str | None,
        typer.Option(help="Run in this sandbox; defaults to agent config or host."),
    ] = None,
    allows: Annotated[
        list[str] | None,
        typer.Option("--allow", help="Set DOMAIN=SELECTORS. Repeat by domain."),
    ] = None,
    limits: Annotated[
        list[str] | None,
        typer.Option(
            "--limit",
            help="Set FIELD=VALUE. Repeat for another field.",
        ),
    ] = None,
    defaults: Annotated[
        list[str] | None,
        typer.Option("--default", help="Set FIELD=VALUE. Repeat for another field."),
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
            help=DEVELOPMENT_WHEEL_HELP,
        ),
    ] = None,
    endpoint_host: Annotated[
        str | None,
        typer.Option("--endpoint-host", help="Endpoint host name.", hidden=True),
    ] = None,
) -> None:
    from toolang.up import sandbox as sandbox_runtime

    selector = require_runtime_agent(ctx, agent)
    if user_call(agents.parse_agent_selector, selector).form != "name":
        raise click.ClickException(
            "start only supports local agent names; clone the remote source first"
        )
    progress = command_progress(ctx)
    try:
        target = agents.resolve_run_layout(
            context_root(ctx),
            selector,
            progress=progress.sink,
        )
        launch = resolve_startup(
            ctx,
            target,
            sandbox=sandbox,
            allows=allows,
            defaults=defaults,
            limits=limits,
            inboxes=inboxes,
            port=port,
            host=host,
            endpoint_host=endpoint_host,
            dev=dev,
            background=True,
            progress=progress.sink,
        )
    except KeyboardInterrupt:
        progress.close()
        raise typer.Exit(130) from None
    except click.ClickException:
        progress.close()
        raise

    warn_development_package_source(launch.startup, progress=progress)
    try:
        handle = user_call(
            asyncio.run,
            sandbox_runtime.launch(
                launch.startup,
                progress=progress.sink,
            ),
        )
    except KeyboardInterrupt:
        progress.close()
        raise typer.Exit(130) from None
    except (
        TimeoutError,
        RuntimeError,
        OSError,
        ValueError,
        click.ClickException,
    ) as exc:
        progress.close()
        raise click.ClickException(
            runtime_startup_failure_message(
                progress,
                exc,
                log_path=launch.target.runtime_log,
                dev_artifact=launch.startup.dev_artifact,
                development_build=development_source()[0],
            )
        ) from exc
    progress.close()
    typer.echo(f"Agent {launch.target.name} started: {handle.state.ref.endpoint}")


def stop(
    ctx: typer.Context,
    agent: str | None = typer.Argument(None, help="Agent name", hidden=True),
    force: Annotated[
        bool,
        typer.Option(help="Force-stop when graceful shutdown does not complete."),
    ] = False,
) -> None:
    from toolang.up import sandbox as sandbox_runtime
    from ...common.progress import make_cli_progress

    agent_name = require_runtime_agent(ctx, agent)
    root = context_root(ctx)
    layout = AgentLayout.resident(root, agent_name)
    progress = make_cli_progress()
    try:
        stopped = user_call(
            asyncio.run,
            sandbox_runtime.stop(layout, force=force, progress=progress.sink),
        )
    except KeyboardInterrupt:
        progress.close()
        raise typer.Exit(130) from None
    finally:
        progress.close()
    if not stopped:
        raise click.ClickException(f"Agent {agent_name} not running")
    typer.echo(f"Agent {agent_name} stopped")


def serve(
    ctx: typer.Context,
    agent: Annotated[str, typer.Argument(help="Agent name.")],
    host: Annotated[str, typer.Option(help="API bind host.")] = "127.0.0.1",
    endpoint_host: Annotated[
        str | None,
        typer.Option("--endpoint-host", help="Externally visible endpoint host."),
    ] = None,
    port: Annotated[int, typer.Option(help="API bind port.")] = 7001,
    allows: Annotated[
        list[str] | None,
        typer.Option("--allow", help="Set DOMAIN=SELECTORS. Repeat by domain."),
    ] = None,
    limits: Annotated[
        list[str] | None,
        typer.Option(
            "--limit",
            help="Set FIELD=VALUE. Repeat for another field.",
        ),
    ] = None,
    defaults: Annotated[
        list[str] | None,
        typer.Option("--default", help="Set FIELD=VALUE. Repeat for another field."),
    ] = None,
    inboxes: Annotated[
        list[Path] | None,
        typer.Option("--inbox", help="Watch a file inbox. Repeat to watch more."),
    ] = None,
    log_spec: Annotated[
        str | None,
        typer.Option("--log", help="Python logging specification."),
    ] = None,
) -> None:
    """Run the internal AgentServer entrypoint."""

    from toolang.up.server import resolve_serve, serve as serve_agent

    layout = AgentLayout.resident(context_root(ctx), agent)
    sandbox = os.environ.get("TOOLANG_SANDBOX", "host").strip() or "host"
    environ = load_runtime_environ(layout, base_environ=os.environ)
    environ["TOOLANG_ROOT"] = str(layout.root)
    if model_catalog := context_model_catalog(ctx):
        environ[MODEL_CATALOG_ENV] = str(model_catalog)
    spec = user_call(
        resolve_serve,
        layout=layout,
        host=host,
        endpoint_host=endpoint_host,
        port=port,
        ceiling_overrides=user_call(resolve_ceiling_overrides, {}, allows),
        binding_overrides=user_call(resolve_binding_overrides, {}, defaults),
        limit_overrides=user_call(resolve_limit_overrides, {}, limits),
        file_inboxes=inboxes,
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
    ctx: typer.Context,
    target: AgentLayout,
    *,
    sandbox: str | None,
    allows: list[str] | None,
    defaults: list[str] | None,
    limits: list[str] | None,
    inboxes: list[Path] | None,
    port: int | None,
    host: str,
    endpoint_host: str | None,
    dev: Path | None,
    background: bool,
    progress: ProgressSink | None = None,
) -> RuntimeLaunch:
    from toolang.up import sandbox as sandbox_runtime

    root, agent = target.root, target.name
    if target.placement == "resident" and not target.home.is_dir():
        raise click.ClickException(f"Agent {agent} not found")
    existing = agents.AgentProcess(target).status(ui_base_url=ui_base_url())
    if existing is not None and existing.status in {"running", "preparing", "starting"}:
        raise click.ClickException(active_agent_error(existing))
    environ = load_runtime_environ(target, base_environ=os.environ)
    environ["TOOLANG_ROOT"] = str(root)
    if model_catalog := context_model_catalog(ctx):
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
    binding_overrides = user_call(
        resolve_binding_overrides,
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
            binding_overrides=binding_overrides,
            limit_overrides=limit_overrides,
            file_inboxes=inboxes,
            dev=dev,
            log_spec=log_plan.spec,
            output="file",
            log_path=log_plan.path if background else target.runtime_log,
            temporary_port=target.placement == "visiting" and port is None,
            environ=log_plan.environ,
            progress=progress,
        ),
    )
    return RuntimeLaunch(target, startup, log_plan.environ, log_plan)
