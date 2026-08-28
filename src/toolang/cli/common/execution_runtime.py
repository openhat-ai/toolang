"""CLI ownership of one embedded or AgentServer execution runtime."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
import os
from pathlib import Path
import sys
from typing import Literal

from toolang.base.errors import ToolangError
from toolang.common.layout import AgentLayout
from toolang.common.version import development_source
from toolang.plugin.models.catalog import MODEL_CATALOG_ENV
from toolang.up import process as agents
from toolang.up import sandbox as sandbox_runtime
from toolang.up.logging import resolve_agent_logging

from .context import load_runtime_environ
from .progress import make_cli_progress, runtime_startup_failure_message


ExecutionMode = Literal["embedded", "remote"]
DEVELOPMENT_WHEEL_HELP = (
    "Install Toolang in a new guest from a wheel; directories select the newest "
    "Toolang wheel recursively."
)


class ExecutionRuntimeError(RuntimeError):
    """One execution-runtime selection or lifecycle failure."""


@dataclass(frozen=True, slots=True)
class ExecutionRuntime:
    """Resolved location and ownership of one command's executor."""

    sandbox: str
    mode: ExecutionMode
    endpoint: str | None = None
    owned: bool = False

    def __post_init__(self) -> None:
        sandbox = self.sandbox.strip()
        if not sandbox or sandbox != self.sandbox:
            raise ValueError("execution runtime requires a canonical sandbox")
        if self.mode == "embedded":
            if self.endpoint is not None or self.owned:
                raise ValueError("embedded execution cannot own a remote endpoint")
            if sandbox != "host":
                raise ValueError("embedded execution requires the host sandbox")
            return
        if self.mode != "remote":  # pragma: no cover - closed type at call sites
            raise ValueError(f"unknown execution runtime mode: {self.mode}")
        if self.endpoint is None or not self.endpoint.strip():
            raise ValueError("remote execution requires an endpoint")
        if self.endpoint != self.endpoint.strip():
            raise ValueError("remote execution requires a canonical endpoint")


@contextmanager
def open_execution_runtime(
    layout: AgentLayout,
    *,
    sandbox: str | None,
    dev: Path | None = None,
    model_catalog: Path | None = None,
    ui_base_url: str = "",
    base_environ: Mapping[str, str] | None = None,
    show_progress: bool = True,
) -> Iterator[ExecutionRuntime]:
    """Attach to, embed, or temporarily launch one execution runtime."""

    status = agents.AgentProcess(layout).status(ui_base_url=ui_base_url)
    if status is not None and status.status in {"preparing", "starting"}:
        raise ExecutionRuntimeError(
            f"agent {layout.name} is {status.status}; wait for it to become ready"
        )
    if status is not None and status.status == "running":
        if dev is not None:
            raise ExecutionRuntimeError(
                f"--dev only applies when starting a new guest; agent {layout.name} "
                "is already running. Stop it first or omit --dev."
            )
        yield _attached_runtime(layout, status, requested=sandbox)
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
        raise ExecutionRuntimeError(str(exc)) from exc
    if selected == "host":
        if dev is not None:
            raise ExecutionRuntimeError(
                "--dev only applies to guest sandboxes; host uses the current "
                "Toolang installation."
            )
        progress = make_cli_progress(agent=layout.name, enabled=show_progress)
        try:
            asyncio.run(sandbox_runtime.release_stopped(layout, progress=progress))
        except (
            ImportError,
            OSError,
            RuntimeError,
            ToolangError,
            TypeError,
            ValueError,
        ) as exc:
            raise ExecutionRuntimeError(str(exc)) from exc
        finally:
            progress.finish(details=False)
        yield ExecutionRuntime(sandbox="host", mode="embedded")
        return

    launch = _resolve_inactive_launch(
        layout,
        sandbox=selected,
        dev=dev,
        model_catalog=model_catalog,
        base_environ=base_environ,
    )
    warn_development_package_source(launch)

    progress = make_cli_progress(
        agent=layout.name,
        sandbox=launch.sandbox,
        enabled=show_progress,
    )
    launch_cleanup_progress = make_cli_progress(
        agent=layout.name,
        sandbox=launch.sandbox,
        enabled=show_progress,
    )
    try:
        handle = asyncio.run(
            sandbox_runtime.launch(
                launch,
                progress=progress,
                cleanup_progress=launch_cleanup_progress,
            )
        )
    except KeyboardInterrupt:
        progress.interrupt()
        launch_cleanup_progress.finish(details=False)
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
        launch_cleanup_progress.finish(details=False)
        raise ExecutionRuntimeError(
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
    launch_cleanup_progress.finish(details=False)

    runtime: ExecutionRuntime | None = None
    body_error: BaseException | None = None
    try:
        runtime = ExecutionRuntime(
            sandbox=handle.state.sandbox,
            mode="remote",
            endpoint=handle.state.ref.endpoint,
            owned=True,
        )
        yield runtime
    except BaseException as exc:
        body_error = exc
        if runtime is None and isinstance(exc, ValueError):
            raise ExecutionRuntimeError(str(exc)) from exc
        raise
    finally:
        shutdown_progress = make_cli_progress(
            agent=layout.name,
            sandbox=handle.state.sandbox,
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
                raise ExecutionRuntimeError(message) from exc
        finally:
            shutdown_progress.finish()


def sandbox_matches(requested: str, running: str) -> bool:
    """Return whether one explicit selector accepts a canonical runtime."""

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


def _attached_runtime(
    layout: AgentLayout,
    status: agents.AgentStatus,
    *,
    requested: str | None,
) -> ExecutionRuntime:
    if status.endpoint is None or status.sandbox is None:
        raise ExecutionRuntimeError(
            f"running agent {layout.name} has incomplete runtime status"
        )
    if requested is not None and not sandbox_matches(requested, status.sandbox):
        raise ExecutionRuntimeError(
            f"--sandbox {requested} does not match running sandbox {status.sandbox}"
        )
    return ExecutionRuntime(
        sandbox=status.sandbox,
        mode="remote",
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
        raise ExecutionRuntimeError(str(exc)) from exc
