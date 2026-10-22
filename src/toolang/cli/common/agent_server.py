"""CLI acquisition of an AgentServer for run execution."""

from __future__ import annotations

import asyncio

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import replace
import os
from pathlib import Path
from typing import cast
import sys
import time

import httpx

from toolang.api.version import AGENT_API_VERSION
from toolang.base.errors import ToolangError
from toolang.base.types.model import ModelOverride
from toolang.common.layout import AgentLayout
from toolang.common.version import development_source
from toolang.up import process as agents
from toolang.up.instance import read_instance
from toolang.up.workload import owns_preparation
from toolang.up import sandbox as sandbox_runtime
from toolang.up.types import (
    AgentServerRef,
    DriverName,
    ExecutionDrivers,
    ServerLifetime,
)
from toolang.up.config import placement_policy

from .context import load_runtime_environ, resolve_root
from .server_lease import ServerLease
from .parameters import DEVELOPMENT_WHEEL_HELP as DEVELOPMENT_WHEEL_HELP
from .startup import resolve_attachment_requirements, resolve_runtime_launch
from .progress import (
    CliProgress,
    make_cli_progress,
    runtime_startup_failure_message,
)


AGENT_READY_TIMEOUT_SEC = sandbox_runtime.SANDBOX_READY_TIMEOUT_SEC


class AgentServerAcquisitionError(RuntimeError):
    """One AgentServer selection or lifecycle failure."""


class ServerClosingError(AgentServerAcquisitionError):
    """Acquisition may retry after this owner has finished shutdown."""


@contextmanager
def acquire_agent_server(
    layout: AgentLayout,
    *,
    sandbox: str | None,
    dev: Path | None = None,
    model_catalog: Path | None = None,
    ui_base_url: str = "",
    base_environ: Mapping[str, str] | None = None,
    show_progress: bool = True,
    compact_override: ModelOverride | None = None,
    workspace_additions: Mapping[str, str] | None = None,
    temporary: bool = False,
    drivers: ExecutionDrivers | None = None,
    required_driver: DriverName | None = None,
    attach_only: bool = False,
    expected_launch_id: str | None = None,
    attachment_requirements: Mapping[str, object] | None = None,
    bound: bool = False,
) -> Iterator[AgentServerRef | None]:
    """Acquire an executor using the placement's reuse and ownership policy."""

    # Share the management lock with start/serve/stop, releasing it before caller work.
    lease: ServerLease | None = None
    try:
        with (
            make_cli_progress(enabled=show_progress, preparation=True) as progress,
            sandbox_runtime.management_lock(layout, progress=progress.sink),
        ):
            if expected_launch_id is not None:
                current = read_instance(layout.runtime_status)
                if current is None or current["launch_id"] != expected_launch_id:
                    raise AgentServerAcquisitionError(
                        "selected execution owner was replaced"
                    )
            deadline = time.monotonic() + AGENT_READY_TIMEOUT_SEC
            while True:
                acquired = _prepare_agent_server(
                    layout,
                    sandbox=sandbox,
                    dev=dev,
                    model_catalog=model_catalog,
                    ui_base_url=ui_base_url,
                    base_environ=base_environ,
                    progress=progress,
                    compact_override=compact_override,
                    workspace_additions=workspace_additions,
                    temporary=temporary,
                    drivers=drivers,
                    required_driver=required_driver,
                    attach_only=attach_only,
                    attachment_requirements=attachment_requirements,
                    bound=bound,
                    expected_launch_id=expected_launch_id,
                )
                try:
                    selected_server = (
                        acquired
                        if isinstance(acquired, AgentServerRef)
                        else _server_reference(
                            layout, acquired.state.sandbox, acquired.state.ref.endpoint
                        )
                        if isinstance(acquired, sandbox_runtime.SandboxHandle)
                        else None
                    )
                except ValueError:
                    if isinstance(acquired, sandbox_runtime.SandboxHandle):
                        asyncio.run(
                            sandbox_runtime.stop_handle(
                                layout, acquired, progress=progress.sink
                            )
                        )
                    raise
                if not temporary and selected_server is not None:
                    try:
                        selected_server, lease = _retain_server(selected_server)
                    except ServerClosingError:
                        _wait_for_closing_server(layout, selected_server, deadline)
                        if attach_only:
                            raise
                        continue
                if isinstance(acquired, AgentServerRef):
                    progress.complete_preparation()
                break
    except AgentServerAcquisitionError:
        raise
    except (
        ImportError,
        OSError,
        RuntimeError,
        ToolangError,
        TypeError,
        ValueError,
        httpx.HTTPError,
    ) as exc:
        raise AgentServerAcquisitionError(str(exc)) from exc

    if lease is not None:
        lease.start()
    if not isinstance(acquired, sandbox_runtime.SandboxHandle):
        try:
            yield selected_server
        finally:
            if lease is not None:
                lease.close()
                if sys.exc_info()[0] in {None, KeyboardInterrupt}:
                    lease.check()
        return

    handle = acquired
    server: AgentServerRef | None = None
    body_error: BaseException | None = None
    try:
        server = selected_server
        yield server
    except BaseException as exc:
        body_error = exc
        if server is None and isinstance(exc, ValueError):
            raise AgentServerAcquisitionError(str(exc)) from exc
        raise
    finally:
        if lease is not None:
            lease.close()
            if body_error is None or isinstance(body_error, KeyboardInterrupt):
                lease.check()
        if (
            temporary
            or not placement_policy(layout.placement).reuse_executor
            or server is None
        ):
            shutdown_progress = make_cli_progress(
                enabled=show_progress,
                leading_gap=True,
            )
            try:
                with shutdown_progress:
                    asyncio.run(
                        sandbox_runtime.stop_handle(
                            layout,
                            handle,
                            progress=shutdown_progress.sink,
                        )
                    )
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                message = shutdown_progress.failure_message(
                    exc, log_path=layout.runtime_log
                )
                if body_error is not None:
                    print(message, file=sys.stderr)
                else:
                    raise AgentServerAcquisitionError(message) from exc


def _managed_lifecycle(layout: AgentLayout, state: Mapping[str, object]) -> bool:
    version = state.get("api_version")
    if "api_version" in state and (
        type(version) is not int or version != AGENT_API_VERSION
    ):
        raise AgentServerAcquisitionError(
            f"server API version {version!r} is unsupported; stop and restart it with this Toolang version"
        )
    return version == AGENT_API_VERSION


def _server_reference(
    layout: AgentLayout, sandbox: str, endpoint: str
) -> AgentServerRef:
    entry = read_instance(layout.runtime_status, layout.instance) or {}
    state = entry.get("workload", {})
    managed = _managed_lifecycle(layout, state)
    return AgentServerRef(
        sandbox=sandbox,
        endpoint=endpoint,
        launch_id=str(entry["launch_id"]) if managed else None,
        lifetime=cast(ServerLifetime, state.get("lifetime")) if managed else None,
    )


def _retain_server(server: AgentServerRef) -> tuple[AgentServerRef, ServerLease | None]:
    if server.launch_id is None:
        return server, None
    try:
        with httpx.Client(timeout=3.0) as http:
            response = http.get(f"{server.endpoint}/api/v{AGENT_API_VERSION}/profile")
            response.raise_for_status()
            runtime = response.json()["runtime"]
        if runtime.get("launch_id") != server.launch_id:
            raise AgentServerAcquisitionError("server launch changed; acquire it again")
        server = replace(server, lifetime=runtime["lifetime"])
        if server.lifetime != "automatic":
            return server, None
        lease = ServerLease(server)
        lease.register()
    except httpx.TransportError as exc:
        if server.lifetime != "automatic":
            raise
        raise ServerClosingError(
            "server stopped during acquisition; acquire it again"
        ) from exc
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 409 and "closing" in exc.response.text:
            raise ServerClosingError("server is closing; wait for it to stop") from exc
        raise
    return replace(
        server, client_id=lease.client_id, check_retention=lease.check
    ), lease


def _wait_for_closing_server(
    layout: AgentLayout, server: AgentServerRef, deadline: float
) -> None:
    """Wait for the selected workload to exit, including its shutdown cleanup."""

    while True:
        reference = agents.AgentProcess(layout).reference
        if reference is None or reference.launch_id != server.launch_id:
            return
        implementation = sandbox_runtime.load_state_sandbox(layout, reference)
        try:
            running = asyncio.run(
                asyncio.wait_for(
                    implementation.running(reference.ref),
                    timeout=max(0.0, deadline - time.monotonic()),
                )
            )
        except TimeoutError as exc:
            raise AgentServerAcquisitionError(
                "server shutdown did not complete"
            ) from exc
        if not running:
            return
        if time.monotonic() >= deadline:
            raise AgentServerAcquisitionError("server shutdown did not complete")
        time.sleep(0.05)


def _prepare_agent_server(
    layout: AgentLayout,
    *,
    sandbox: str | None,
    dev: Path | None,
    model_catalog: Path | None,
    ui_base_url: str,
    base_environ: Mapping[str, str] | None,
    progress: CliProgress,
    compact_override: ModelOverride | None,
    workspace_additions: Mapping[str, str] | None,
    temporary: bool,
    drivers: ExecutionDrivers | None = None,
    required_driver: DriverName | None = None,
    attach_only: bool = False,
    attachment_requirements: Mapping[str, object] | None = None,
    bound: bool = False,
    expected_launch_id: str | None = None,
) -> AgentServerRef | sandbox_runtime.SandboxHandle | None:
    policy = placement_policy(layout.placement)
    entry = read_instance(layout.runtime_status, layout.instance) or {}
    managed = _managed_lifecycle(layout, entry.get("workload", {}))
    if not policy.reuse_executor and not attach_only and not bound:
        if not owns_preparation(layout, entry):
            raise AgentServerAcquisitionError(
                "roaming execution requires a fresh instance owned by this caller; controls may attach to its live owner"
            )
        status = None
    else:
        status = agents.AgentProcess(layout).status(
            # Managed attachment checks the live profile and registers retention below.
            # A separate health wait would mistake idle shutdown for failed startup.
            ui_base_url=ui_base_url,
            check_health=not temporary and not managed,
        )
    if status is not None and status.status in {"preparing", "starting"}:
        if temporary:
            raise AgentServerAcquisitionError(
                f"agent {layout.name} is {status.status}; wait for it to become ready"
            )
        status = agents.AgentProcess(layout).wait_ready(
            status,
            ui_base_url=ui_base_url,
            timeout=AGENT_READY_TIMEOUT_SEC,
            progress=progress.sink,
        )
    if status is not None and status.status == "running":
        from toolang.up.identity import verify_identity

        entry = read_instance(layout.runtime_status, layout.instance) or {}
        if (
            expected_launch_id is not None
            and entry.get("launch_id") != expected_launch_id
        ):
            raise AgentServerAcquisitionError("selected execution owner was replaced")
        verify_identity(layout, entry.get("control", {}))
        attached = _attached_server(layout, status, requested=sandbox)
        from toolang.up.contract import bound_drivers, require_compatible

        if drivers is not None or required_driver is not None:
            running_drivers = bound_drivers(layout)
            if drivers is not None and drivers != running_drivers:
                raise AgentServerAcquisitionError(
                    "--enable differs from the running agent's drivers; stop and restart the agent"
                )
            if required_driver is not None and required_driver not in running_drivers:
                raise AgentServerAcquisitionError(
                    f"{required_driver} driver is disabled"
                )
        require_compatible(
            layout,
            resolve_attachment_requirements(
                sandbox=attached.sandbox,
                dev=dev,
                model_catalog=model_catalog,
                compact_override=compact_override,
                workspace_additions=workspace_additions,
            ),
            attachment_requirements or {},
        )
        return attached

    if attach_only:
        raise AgentServerAcquisitionError(
            "run has no healthy live execution owner; no server was started"
        )

    if bound:
        current = read_instance(layout.runtime_status, layout.instance) or {}
        if (
            expected_launch_id is not None
            and current.get("launch_id") != expected_launch_id
        ):
            raise AgentServerAcquisitionError("selected execution owner was replaced")
        if not entry or status is None or status.status not in {"stopped", "failed"}:
            raise AgentServerAcquisitionError(
                "selected instance has no conclusively stopped owner"
            )
        if not layout.program.is_file():
            raise AgentServerAcquisitionError(
                "selected instance is missing its program; no replacement source was fetched"
            )
    else:
        layout = _prepare_source(layout, progress)

    effective_drivers: ExecutionDrivers = (
        drivers
        if drivers is not None
        else frozenset()
        if temporary
        else policy.client_drivers
    )
    if required_driver is not None and required_driver not in effective_drivers:
        raise AgentServerAcquisitionError(
            f"{required_driver} driver is required; include it in --enable"
        )
    selected = sandbox_runtime.resolve_selection(layout, explicit=sandbox)
    if selected == "host" and dev is not None:
        raise AgentServerAcquisitionError(
            "--dev only applies to guest sandboxes; host uses the current "
            "Toolang installation."
        )
    if selected == "host" and temporary:
        asyncio.run(sandbox_runtime.release_stopped(layout))
        return None

    launch = _resolve_launch(
        layout,
        sandbox=selected,
        dev=dev,
        model_catalog=model_catalog,
        base_environ=base_environ,
        compact_override=compact_override,
        workspace_additions=workspace_additions,
        temporary_port=temporary,
        drivers=effective_drivers,
        lifetime=policy.client_lifetime,
    )
    with progress.suspended():
        warn_development_package_source(launch)

    try:
        handle = asyncio.run(sandbox_runtime.launch(launch, progress=progress.sink))
    except KeyboardInterrupt:
        raise
    except (
        ImportError,
        OSError,
        RuntimeError,
        ToolangError,
        TypeError,
        ValueError,
    ) as exc:
        raise AgentServerAcquisitionError(
            runtime_startup_failure_message(
                progress,
                exc,
                log_path=layout.runtime_log,
                dev_artifact=launch.dev_artifact,
                development_build=development_source()[0],
            )
        ) from exc
    return handle


def _prepare_source(layout: AgentLayout, progress: CliProgress) -> AgentLayout:
    from toolang.up.source import prepare_visiting_layout

    try:
        return prepare_visiting_layout(
            layout,
            toolang_root=layout.toolang_root or resolve_root(None),
            progress=progress.sink,
        )
    except (OSError, RuntimeError, ToolangError, TypeError, ValueError) as exc:
        raise AgentServerAcquisitionError(progress.failure_message(exc)) from exc


def sandbox_matches(requested: str, running: str) -> bool:
    """Return whether one explicit selector accepts a running AgentServer."""

    requested_name, separator, _requested_spec = requested.partition(":")
    running_name = running.partition(":")[0]
    if requested_name.strip() != running_name.strip():
        return False
    return not separator or requested.strip() == running.strip()


def warn_development_package_source(
    launch: sandbox_runtime.LaunchSpec,
) -> None:
    """Warn when a development CLI starts a guest from the package index."""

    if launch.dev_artifact is not None or launch.sandbox.partition(":")[0] == "host":
        return
    detected, source = development_source()
    if not detected:
        return
    sandbox_name = launch.sandbox.partition(":")[0]
    if source is None:
        warning = (
            "Warning: the current Toolang process is a development build, but the "
            f"new {sandbox_name} guest will install Toolang from the package index."
        )
    else:
        warning = (
            f"Warning: the new {sandbox_name} guest will install Toolang from the "
            f"package index, not from {source}."
        )
    print(warning, file=sys.stderr)
    print(
        "Build the current source with `uv build --wheel`, then run this command "
        "again with `--dev dist`.",
        file=sys.stderr,
    )


def _attached_server(
    layout: AgentLayout,
    status: agents.AgentStatus,
    *,
    requested: str | None,
) -> AgentServerRef:
    if status.endpoint is None or status.sandbox is None:
        raise AgentServerAcquisitionError(
            f"running agent {layout.name} has incomplete runtime status"
        )
    if requested is not None and not sandbox_matches(requested, status.sandbox):
        raise AgentServerAcquisitionError(
            f"--sandbox {requested} does not match running sandbox {status.sandbox}"
        )
    return _server_reference(layout, status.sandbox, status.endpoint.strip())


def _resolve_launch(
    layout: AgentLayout,
    *,
    sandbox: str | None,
    dev: Path | None,
    model_catalog: Path | None,
    base_environ: Mapping[str, str] | None,
    compact_override: ModelOverride | None = None,
    workspace_additions: Mapping[str, str] | None = None,
    temporary_port: bool = False,
    lifetime: ServerLifetime | None = None,
    drivers: ExecutionDrivers | None = None,
    active_port: int | None = None,
) -> sandbox_runtime.LaunchSpec:
    try:
        environ = load_runtime_environ(
            layout,
            base_environ=os.environ if base_environ is None else base_environ,
        )
        toolang_root = layout.toolang_root or (
            layout.root
            if layout.placement == "resident"
            else resolve_root(None, environ=environ)
        )
        lifetime = lifetime or placement_policy(layout.placement).client_lifetime
        if temporary_port and lifetime == "automatic":
            lifetime = "persistent"
        return resolve_runtime_launch(
            layout,
            toolang_root=toolang_root,
            environ=environ,
            sandbox=sandbox,
            dev=dev,
            model_catalog=model_catalog,
            drivers=drivers,
            lifetime=lifetime,
            background=True,
            temporary_port=temporary_port,
            active_port=active_port,
            compact_override=compact_override,
            workspace_additions=workspace_additions,
        )
    except (
        ImportError,
        OSError,
        RuntimeError,
        ToolangError,
        TypeError,
        ValueError,
    ) as exc:
        raise AgentServerAcquisitionError(str(exc)) from exc
