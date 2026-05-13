"""Shared sandbox value types."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

SandboxStartMode = Literal["direct", "managed"]


@dataclass(frozen=True, slots=True)
class SandboxSelector:
    """One resolved sandbox selector such as ``none`` or ``docker:python-3.13``."""

    driver: str
    target: str | None = None

    @classmethod
    def parse(cls, raw: str) -> "SandboxSelector":
        value = raw.strip()
        if not value:
            raise ValueError("sandbox selector cannot be empty")
        driver, sep, target = value.partition(":")
        parsed_driver = driver.strip()
        if not parsed_driver:
            raise ValueError("sandbox selector is missing driver")
        parsed_target = target.strip() if sep else ""
        return cls(driver=parsed_driver, target=parsed_target or None)

    def render(self) -> str:
        if self.target is None:
            return self.driver
        return f"{self.driver}:{self.target}"

    def to_data(self) -> dict[str, object]:
        return {
            "driver": self.driver,
            "target": self.target,
        }

    @classmethod
    def from_data(cls, payload: object) -> "SandboxSelector":
        if not isinstance(payload, dict):
            raise ValueError("sandbox selector data must be a mapping")
        data = {str(key): value for key, value in payload.items()}
        driver = data.get("driver")
        if not isinstance(driver, str) or not driver.strip():
            raise ValueError("sandbox selector data is missing driver")
        raw_target = data.get("target")
        target = raw_target.strip() if isinstance(raw_target, str) and raw_target.strip() else None
        return cls(driver=driver.strip(), target=target)


@dataclass(frozen=True, slots=True)
class SandboxMount:
    """One local-to-sandbox mount."""

    local_path: Path
    sandbox_path: Path
    read_only: bool = False

    def to_data(self) -> dict[str, object]:
        return {
            "local_path": str(self.local_path),
            "sandbox_path": str(self.sandbox_path),
            "read_only": self.read_only,
        }


@dataclass(frozen=True, slots=True)
class SandboxPortForward:
    """One local-to-sandbox port forward."""

    bind_host: str
    local_port: int
    sandbox_port: int

    def to_data(self) -> dict[str, object]:
        return {
            "bind_host": self.bind_host,
            "local_port": self.local_port,
            "sandbox_port": self.sandbox_port,
        }


@dataclass(frozen=True, slots=True)
class SandboxStartRequest:
    """One explicit sandbox launch request resolved by the CLI."""

    selector: SandboxSelector
    local_root: Path
    local_home: Path
    sandbox_root: Path
    sandbox_home: Path
    agent_name: str
    bind_host: str
    endpoint_host: str
    port: int
    endpoint: str
    feature_names: tuple[str, ...] = ()
    run_command: tuple[str, ...] = ()
    run_shell_command: str | None = None
    env_vars: dict[str, str] = field(default_factory=dict)
    local_dev_artifact: Path | None = None


@dataclass(frozen=True, slots=True)
class SandboxState:
    """One plugin-owned sandbox runtime state snapshot."""

    selector: SandboxSelector
    runtime_id: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    def to_data(self) -> dict[str, object]:
        return {
            "selector": self.selector.to_data(),
            "runtime_id": self.runtime_id,
            "meta": dict(self.meta),
        }

    @classmethod
    def from_data(cls, payload: object) -> "SandboxState":
        if not isinstance(payload, dict):
            raise ValueError("sandbox state data must be a mapping")
        data = {str(key): value for key, value in payload.items()}
        runtime_id = data.get("runtime_id")
        selector = SandboxSelector.from_data(data.get("selector"))
        meta = data.get("meta")
        if isinstance(meta, dict):
            meta_data = {str(key): value for key, value in meta.items()}
        else:
            meta_data = {}
        return cls(
            selector=selector,
            runtime_id=runtime_id if isinstance(runtime_id, str) and runtime_id.strip() else None,
            meta=meta_data,
        )


@dataclass(frozen=True, slots=True)
class SandboxPlan:
    """One prepared launch plan ready to start inside one sandbox."""

    selector: SandboxSelector
    start_mode: SandboxStartMode
    sandbox_root: Path
    sandbox_home: Path
    sandbox_working_directory: Path
    run_command: tuple[str, ...] = ()
    run_shell_command: str | None = None
    mounts: tuple[SandboxMount, ...] = ()
    port_forwards: tuple[SandboxPortForward, ...] = ()
    env_vars: dict[str, str] = field(default_factory=dict)
    sandbox_dev_artifact: Path | None = None
    state: SandboxState | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    def to_data(self) -> dict[str, object]:
        return {
            "selector": self.selector.to_data(),
            "start_mode": self.start_mode,
            "sandbox_root": str(self.sandbox_root),
            "sandbox_home": str(self.sandbox_home),
            "sandbox_working_directory": str(self.sandbox_working_directory),
            "run_command": list(self.run_command),
            "run_shell_command": self.run_shell_command,
            "mounts": [item.to_data() for item in self.mounts],
            "port_forwards": [item.to_data() for item in self.port_forwards],
            "env_vars": dict(self.env_vars),
            "sandbox_dev_artifact": (
                str(self.sandbox_dev_artifact) if self.sandbox_dev_artifact is not None else None
            ),
            "state": self.state.to_data() if self.state is not None else None,
            "meta": dict(self.meta),
        }


@dataclass(frozen=True, slots=True)
class SandboxStartResult:
    """One sandbox start result."""

    state: SandboxState
    endpoint: str | None = None
    message: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    def to_data(self) -> dict[str, object]:
        return {
            "state": self.state.to_data(),
            "endpoint": self.endpoint,
            "message": self.message,
            "meta": dict(self.meta),
        }
