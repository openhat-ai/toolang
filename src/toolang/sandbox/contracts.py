"""Contracts for Toolang sandbox plugins."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

from toolang.agent.prepared import PreparedAgent
from toolang.concepts.execution import RuntimeLoop
from toolang.concepts.sandbox import SandboxSpec, SandboxState


@dataclass(frozen=True, slots=True)
class StartedSandbox:
    """Result of starting one sandboxed agent runtime."""

    state: SandboxState
    process: subprocess.Popen | None = None


@dataclass(frozen=True, slots=True)
class SandboxStartRequest:
    """One explicit sandbox start request."""

    spec: SandboxSpec
    prepared: PreparedAgent
    toolang_root: Path
    host: str
    port: int
    endpoint: str
    log_path: Path
    runtime_loops: tuple[RuntimeLoop, ...] = ("server",)
    forward_env_names: tuple[str, ...] = ()


class SandboxPlugin(Protocol):
    """Protocol implemented by one loaded sandbox plugin instance."""

    def start(self, request: SandboxStartRequest) -> StartedSandbox:
        """Start one sandboxed runtime."""

    def alive(self, state: SandboxState) -> bool:
        """Return whether one sandbox runtime appears alive."""

    def stop(
        self,
        state: SandboxState,
        *,
        pid: int | None = None,
        force: bool = False,
    ) -> None:
        """Stop one running sandbox."""


SandboxPluginFactory = Callable[[dict[str, Any]], SandboxPlugin]
