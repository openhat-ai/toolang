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
    context_model_catalog,
    context_root,
    require_runtime_agent,
    load_runtime_environ,
    ui_base_url,
    user_call,
)
from ...common.output import active_agent_error, echo_error
from ...common.version import development_source

if TYPE_CHECKING:
    from toolang.up.sandbox import SandboxState, LaunchSpec
    from ...common.progress import CliProgress


@dataclass(frozen=True, slots=True)
class RuntimeStartup:
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
                output="inherit",
                log_path=log_plan.path,
                temporary_port=options.port is None,
                environ=log_plan.environ,
            ),
        )
        _warn_development_sandbox_package(startup, dev=options.dev)
        return user_call(
            asyncio.run,
            sandbox_runtime.run(
                startup,
                on_ready=lambda state: _report_foreground_ready(
                    layout.name,
                    state,
                ),
            ),
        )
    except KeyboardInterrupt:
        return 130
    except (
        FileExistsError,
        FileNotFoundError,
        OSError,
        RuntimeError,
        ValueError,
        click.ClickException,
    ) as exc:
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
            help="Use a Toolang wheel, or the newest wheel found recursively in a directory.",
        ),
    ] = None,
    endpoint_host: Annotated[
        str | None,
        typer.Option("--endpoint-host", help="Endpoint host name.", hidden=True),
    ] = None,
) -> None:
    from toolang.up import sandbox as sandbox_runtime
    from ...common.progress import as_progress_sink, make_cli_progress

    selector = require_runtime_agent(ctx, agent)
    progress = make_cli_progress()
    finished = False
    try:
        selected_layout = cli_context(ctx).layout
        target = (
            selected_layout
            if selected_layout is not None
            else agents.resolve_run_layout(
                context_root(ctx),
                selector,
                progress=as_progress_sink(progress),
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
            progress=progress,
        )
        progress.finish(details=False)
        finished = True
        _warn_development_sandbox_package(launch.startup, dev=dev)
        exit_code = user_call(
            asyncio.run,
            sandbox_runtime.run(
                launch.startup,
                on_ready=lambda state: _report_foreground_ready(
                    launch.target.name,
                    state,
                ),
            ),
        )
    except KeyboardInterrupt:
        if not finished:
            progress.interrupt()
        raise typer.Exit(130) from None
    except (
        FileExistsError,
        FileNotFoundError,
        OSError,
        RuntimeError,
        ValueError,
        click.ClickException,
    ) as exc:
        if not finished:
            progress.finish(details=False)
        if isinstance(exc, click.ClickException):
            raise
        raise click.ClickException(str(exc)) from exc
    raise typer.Exit(exit_code)


def _report_foreground_ready(name: str, state: SandboxState) -> None:
    typer.echo(
        f"Running agent {name}: {state.ref.endpoint} (Ctrl+C to stop)",
        err=True,
    )


def _warn_development_sandbox_package(
    startup: LaunchSpec,
    *,
    dev: Path | None,
) -> None:
    if dev is not None or startup.sandbox.partition(":")[0] == "host":
        return
    detected, source = development_source()
    if not detected:
        return
    location = f" at {source}" if source is not None else ""
    typer.echo(
        "Warning: the current Toolang process is running from development source"
        f"{location}, but sandbox {startup.sandbox} will install Toolang from the "
        "package index and may run a different version. Build a wheel with "
        "`uv build --wheel` and pass `--dev dist`.",
        err=True,
    )


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
            help="Use a Toolang wheel, or the newest wheel found recursively in a directory.",
        ),
    ] = None,
    endpoint_host: Annotated[
        str | None,
        typer.Option("--endpoint-host", help="Endpoint host name.", hidden=True),
    ] = None,
) -> None:
    from toolang.up import sandbox as sandbox_runtime
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
            allows=allows,
            defaults=defaults,
            limits=limits,
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
    _warn_development_sandbox_package(launch.startup, dev=dev)
    try:
        handle = user_call(asyncio.run, sandbox_runtime.launch(launch.startup))
    except TimeoutError as exc:
        raise click.ClickException(
            f"Agent {launch.target.name} start timed out: {launch.target.runtime_log}"
        ) from exc
    except (RuntimeError, OSError) as exc:
        raise click.ClickException(
            f"Agent {launch.target.name} failed to start: {launch.target.runtime_log}"
        ) from exc
    typer.echo(f"Started agent {launch.target.name}: {handle.state.ref.endpoint}")


def stop(
    ctx: typer.Context,
    agent: str | None = typer.Argument(None, help="Agent name", hidden=True),
    force: Annotated[
        bool,
        typer.Option(help="Force-stop when graceful shutdown does not complete."),
    ] = False,
) -> None:
    from toolang.up import sandbox as sandbox_runtime

    agent_name = require_runtime_agent(ctx, agent)
    root = context_root(ctx)
    layout = AgentLayout.resident(root, agent_name)
    stopped = user_call(
        asyncio.run,
        sandbox_runtime.stop(layout, force=force),
    )
    if not stopped:
        raise click.ClickException(f"Agent {agent_name} not running")
    typer.echo(f"Stopped agent {agent_name}")


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
    raise typer.Exit(user_call(serve_agent, spec, environ=environ))


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
    progress: CliProgress | None,
) -> RuntimeStartup:
    from toolang.up import sandbox as sandbox_runtime

    root, agent = target.root, target.name
    del progress
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
            output="file" if background else "inherit",
            log_path=log_plan.path,
            temporary_port=target.placement == "visiting" and port is None,
            environ=log_plan.environ,
        ),
    )
    return RuntimeStartup(target, startup, log_plan.environ, log_plan)
