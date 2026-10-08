"""CLI acquisition of an AgentServer for run execution."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
import os
from pathlib import Path
import sys
import time

from toolang.base.errors import ToolangError
from toolang.base.types.model import ModelOverride
from toolang.common.files import file_write_lock
from toolang.common.layout import AgentLayout
from toolang.common.version import development_source
from toolang.plugin.catalogs.models_dev.path import MODEL_CATALOG_ENV
from toolang.up import process as agents
from toolang.up import sandbox as sandbox_runtime
from toolang.up.logging import resolve_agent_logging
from toolang.up.types import AgentServerRef

from .context import load_runtime_environ
from .parameters import DEVELOPMENT_WHEEL_HELP as DEVELOPMENT_WHEEL_HELP
from .policy import (
    resolve_ceiling_overrides,
    resolve_compact_override,
    resolve_default_overrides,
    resolve_limit_overrides,
)
from .ports import agent_port
from .progress import (
    make_cli_progress,
    runtime_startup_failure_message,
)


AGENT_READY_TIMEOUT_SEC = sandbox_runtime.SANDBOX_READY_TIMEOUT_SEC


class AgentServerAcquisitionError(RuntimeError):
    """One AgentServer selection or lifecycle failure."""


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
) -> Iterator[AgentServerRef | None]:
    """Ensure a persistent server; temporary scripts retain their existing lifecycle."""

    # Share the management lock with start/serve/stop, releasing it before caller work.
    with file_write_lock(layout.sandbox_state.with_suffix(".lock")):
        acquired = _prepare_agent_server(
            layout,
            sandbox=sandbox,
            dev=dev,
            model_catalog=model_catalog,
            ui_base_url=ui_base_url,
            base_environ=base_environ,
            show_progress=show_progress,
            compact_override=compact_override,
            workspace_additions=workspace_additions,
            temporary=temporary,
        )
    handle = acquired if isinstance(acquired, sandbox_runtime.SandboxHandle) else None
    server: AgentServerRef | None = None
    body_error: BaseException | None = None
    try:
        if isinstance(acquired, sandbox_runtime.SandboxHandle):
            server = AgentServerRef(
                sandbox=acquired.state.sandbox, endpoint=acquired.state.ref.endpoint
            )
        else:
            server = acquired
        yield server
    except BaseException as exc:
        body_error = exc
        if server is None and isinstance(exc, ValueError):
            raise AgentServerAcquisitionError(str(exc)) from exc
        raise
    finally:
        if handle is not None and (temporary or server is None):
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


def _prepare_agent_server(
    layout: AgentLayout,
    *,
    sandbox: str | None,
    dev: Path | None,
    model_catalog: Path | None,
    ui_base_url: str,
    base_environ: Mapping[str, str] | None,
    show_progress: bool,
    compact_override: ModelOverride | None,
    workspace_additions: Mapping[str, str] | None,
    temporary: bool,
) -> AgentServerRef | sandbox_runtime.SandboxHandle | None:
    status = agents.AgentProcess(layout).status(ui_base_url=ui_base_url)
    if status is not None and status.status in {"preparing", "starting"}:
        if temporary:
            raise AgentServerAcquisitionError(
                f"agent {layout.name} is {status.status}; wait for it to become ready"
            )
        deadline = time.monotonic() + AGENT_READY_TIMEOUT_SEC
        while status is not None and status.status in {"preparing", "starting"}:
            if time.monotonic() >= deadline:
                raise AgentServerAcquisitionError(
                    f"agent {layout.name} did not become ready; see {layout.runtime_log}"
                )
            time.sleep(0.1)
            status = agents.AgentProcess(layout).status(ui_base_url=ui_base_url)
        if status is None or status.status != "running":
            raise AgentServerAcquisitionError(
                f"agent {layout.name} stopped before becoming ready; see {layout.runtime_log}"
            )
    if status is not None and status.status == "running":
        if workspace_additions is not None:
            captured = agents.AgentProcess(layout).state() or {}
            if captured.get("workspace_additions", {}) != dict(workspace_additions):
                raise AgentServerAcquisitionError(
                    "workspace bindings differ from the running server; stop it before adding local directories"
                )
        if compact_override is not None:
            raise AgentServerAcquisitionError(
                "--compact-model only applies when starting a runtime; stop the agent first"
            )
        if dev is not None:
            raise AgentServerAcquisitionError(
                f"--dev only applies when starting a new guest; agent {layout.name} "
                "is already running. Stop it first or omit --dev."
            )
        return _attached_server(layout, status, requested=sandbox)

    try:
        selected = sandbox_runtime.resolve_selection(layout, explicit=sandbox)
    except (
        ImportError,
        OSError,
        RuntimeError,
        ToolangError,
        TypeError,
        ValueError,
    ) as exc:
        raise AgentServerAcquisitionError(str(exc)) from exc
    if selected == "host" and dev is not None:
        raise AgentServerAcquisitionError(
            "--dev only applies to guest sandboxes; host uses the current "
            "Toolang installation."
        )
    if selected == "host" and temporary:
        try:
            asyncio.run(sandbox_runtime.release_stopped(layout))
        except (
            ImportError,
            OSError,
            RuntimeError,
            ToolangError,
            TypeError,
            ValueError,
        ) as exc:
            raise AgentServerAcquisitionError(str(exc)) from exc
        return None

    launch = _resolve_inactive_launch(
        layout,
        sandbox=selected,
        dev=dev,
        model_catalog=model_catalog,
        base_environ=base_environ,
        compact_override=compact_override,
        workspace_additions=workspace_additions,
        temporary_port=temporary,
    )
    warn_development_package_source(launch)

    progress = make_cli_progress(enabled=show_progress)
    try:
        with progress:
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
    return AgentServerRef(
        sandbox=status.sandbox,
        endpoint=status.endpoint.strip(),
    )


def _resolve_inactive_launch(
    layout: AgentLayout,
    *,
    sandbox: str | None,
    dev: Path | None,
    model_catalog: Path | None,
    base_environ: Mapping[str, str] | None,
    compact_override: ModelOverride | None = None,
    workspace_additions: Mapping[str, str] | None = None,
    temporary_port: bool = False,
) -> sandbox_runtime.LaunchSpec:
    try:
        environ = load_runtime_environ(
            layout,
            base_environ=os.environ if base_environ is None else base_environ,
        )
        environ["TOOLANG_ROOT"] = str(layout.root)
        if model_catalog is not None:
            environ[MODEL_CATALOG_ENV] = str(model_catalog)
        log_plan = resolve_agent_logging(
            mode="start",
            environ=environ,
            agent_log_path=layout.runtime_log,
        )
        return asyncio.run(
            sandbox_runtime.resolve_launch(
                layout=layout,
                sandbox=sandbox,
                port=None
                if temporary_port
                else agent_port(layout, None, environ=environ),
                dev=dev,
                output="file",
                log_path=log_plan.path,
                log_spec=log_plan.spec,
                temporary_port=temporary_port,
                environ=log_plan.environ,
                ceiling_overrides=resolve_ceiling_overrides(environ),
                default_overrides=resolve_default_overrides(environ),
                limit_overrides=resolve_limit_overrides(environ),
                compact_override=compact_override or resolve_compact_override(environ),
                workspace_additions=workspace_additions,
            )
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
