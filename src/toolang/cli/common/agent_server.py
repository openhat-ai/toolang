"""CLI acquisition of an AgentServer for run execution."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
import os
from pathlib import Path
import sys

from toolang.base.errors import ToolangError
from toolang.common.layout import AgentLayout
from toolang.common.version import development_source
from toolang.plugin.models.catalog import MODEL_CATALOG_ENV
from toolang.up import process as agents
from toolang.up import sandbox as sandbox_runtime
from toolang.up.logging import resolve_agent_logging
from toolang.up.types import AgentServerRef

from .context import load_runtime_environ
from .shutdown_progress import make_runtime_shutdown_progress
from .startup_progress import (
    make_runtime_startup_progress,
    runtime_startup_failure_message,
)


DEVELOPMENT_WHEEL_HELP = (
    "Install Toolang in a new guest from a wheel; directories select the newest "
    "Toolang wheel recursively."
)


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
) -> Iterator[AgentServerRef | None]:
    """Acquire an existing or temporary AgentServer, or select host embedding."""

    status = agents.AgentProcess(layout).status(ui_base_url=ui_base_url)
    if status is not None and status.status in {"preparing", "starting"}:
        raise AgentServerAcquisitionError(
            f"agent {layout.name} is {status.status}; wait for it to become ready"
        )
    if status is not None and status.status == "running":
        if dev is not None:
            raise AgentServerAcquisitionError(
                f"--dev only applies when starting a new guest; agent {layout.name} "
                "is already running. Stop it first or omit --dev."
            )
        yield _attached_server(layout, status, requested=sandbox)
        return

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
    if selected == "host":
        if dev is not None:
            raise AgentServerAcquisitionError(
                "--dev only applies to guest sandboxes; host uses the current "
                "Toolang installation."
            )
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
        yield None
        return

    launch = _resolve_inactive_launch(
        layout,
        sandbox=selected,
        dev=dev,
        model_catalog=model_catalog,
        base_environ=base_environ,
    )
    warn_development_package_source(launch)

    progress = make_runtime_startup_progress(
        layout.name,
        launch.sandbox,
        enabled=show_progress,
    )
    try:
        handle = asyncio.run(
            sandbox_runtime.launch(
                launch,
                progress=progress if show_progress else None,
            )
        )
    except KeyboardInterrupt:
        progress.interrupt()
        raise
    except (
        ImportError,
        OSError,
        RuntimeError,
        ToolangError,
        TypeError,
        ValueError,
    ) as exc:
        progress.finish()
        raise AgentServerAcquisitionError(
            runtime_startup_failure_message(
                layout.name,
                launch.sandbox,
                progress,
                exc,
                log_path=layout.runtime_log,
                dev_artifact=launch.dev_artifact,
                development_build=development_source()[0],
            )
        ) from exc
    progress.finish()

    server: AgentServerRef | None = None
    body_error: BaseException | None = None
    try:
        server = AgentServerRef(
            sandbox=handle.state.sandbox,
            endpoint=handle.state.ref.endpoint,
        )
        yield server
    except BaseException as exc:
        body_error = exc
        if server is None and isinstance(exc, ValueError):
            raise AgentServerAcquisitionError(str(exc)) from exc
        raise
    finally:
        shutdown_progress = make_runtime_shutdown_progress(
            layout.name,
            handle.state.sandbox,
            enabled=show_progress,
        )
        try:
            asyncio.run(
                sandbox_runtime.stop_handle(
                    layout,
                    handle,
                    progress=shutdown_progress,
                )
            )
        except KeyboardInterrupt:
            shutdown_progress.interrupt()
            raise
        except Exception as exc:
            message = (
                f"could not stop temporary agent {layout.name} in "
                f"{handle.state.sandbox}: "
                f"{str(exc).strip() or type(exc).__name__}; "
                f"log: {layout.runtime_log}"
            )
            if body_error is not None:
                print(message, file=sys.stderr)
            else:
                raise AgentServerAcquisitionError(message) from exc
        finally:
            shutdown_progress.finish()


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
                dev=dev,
                output="file",
                log_path=log_plan.path,
                log_spec=log_plan.spec,
                temporary_port=True,
                environ=log_plan.environ,
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
