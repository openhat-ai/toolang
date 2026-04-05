"""Sandbox lifecycle helpers backed by sandbox plugins."""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

from toolang.agent.prepared import PreparedAgent
from toolang.concepts.execution import RuntimeLoop
from toolang.concepts.sandbox import SandboxSpec, SandboxState

from .contracts import SandboxStartRequest, StartedSandbox
from .load import create_sandbox_plugin


def sandbox_alive(state: SandboxState) -> bool:
    """Return whether one sandbox runtime still appears to be alive."""

    return create_sandbox_plugin(state.type).alive(state)


def stop_sandbox(
    state: SandboxState,
    *,
    pid: int | None = None,
    force: bool = False,
) -> None:
    """Stop one running sandbox."""

    create_sandbox_plugin(state.type).stop(state, pid=pid, force=force)


def start_sandbox(
    *,
    spec: SandboxSpec,
    prepared: PreparedAgent,
    toolang_root: Path,
    host: str,
    port: int,
    endpoint: str,
    log_path: Path,
    runtime_loops: tuple[RuntimeLoop, ...] = ("server",),
    forward_env_names: Iterable[str] = (),
) -> StartedSandbox:
    """Start one sandboxed long-lived agent runtime."""

    return create_sandbox_plugin(spec.kind).start(
        SandboxStartRequest(
            spec=spec,
            prepared=prepared,
            toolang_root=toolang_root,
            host=host,
            port=port,
            endpoint=endpoint,
            log_path=log_path,
            runtime_loops=runtime_loops,
            forward_env_names=tuple(forward_env_names),
        )
    )
