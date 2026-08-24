"""Shared hosting value types."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class HostingMount:
    """One local path exposed inside a hosted environment."""

    local_path: Path
    hosted_path: Path
    read_only: bool = False


@dataclass(frozen=True, slots=True)
class HostingPort:
    """One host-to-environment port publication."""

    bind_host: str
    local_port: int
    hosted_port: int


@dataclass(frozen=True, slots=True)
class HostingRequest:
    """Explicit inputs from which one hosting implementation prepares a launch."""

    local_root: Path
    local_home: Path
    hosted_root: Path
    hosted_home: Path
    agent_name: str
    bind_host: str
    endpoint_host: str
    port: int
    endpoint: str
    command: tuple[str, ...]
    working_directory: Path
    log_path: Path | None
    envs: dict[str, str] = field(default_factory=dict)
    mounts: tuple[HostingMount, ...] = ()
    workspaces: dict[str, Path] = field(default_factory=dict)
    workspace_sources: dict[str, Path] = field(default_factory=dict)
    local_dev_artifact: Path | None = None


@dataclass(frozen=True, slots=True)
class HostingPlan:
    """One implementation-owned, fully prepared workload launch."""

    sandbox: str
    command: tuple[str, ...]
    working_directory: Path
    log_path: Path | None
    endpoint: str
    envs: dict[str, str] = field(default_factory=dict)
    mounts: tuple[HostingMount, ...] = ()
    ports: tuple[HostingPort, ...] = ()
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class HostingRef:
    """Serializable reference to one launched workload."""

    runtime_id: str
    endpoint: str
    meta: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        runtime_id = self.runtime_id.strip()
        endpoint = self.endpoint.strip()
        if not runtime_id:
            raise ValueError("hosting reference requires runtime_id")
        if not endpoint:
            raise ValueError("hosting reference requires endpoint")
        object.__setattr__(self, "runtime_id", runtime_id)
        object.__setattr__(self, "endpoint", endpoint)
        object.__setattr__(self, "meta", dict(self.meta))

    def to_data(self) -> dict[str, object]:
        return {
            "runtime_id": self.runtime_id,
            "endpoint": self.endpoint,
            "meta": dict(self.meta),
        }

    @classmethod
    def from_data(cls, payload: object) -> HostingRef:
        if not isinstance(payload, dict):
            raise ValueError("hosting reference must be a mapping")
        data = {str(key): value for key, value in payload.items()}
        runtime_id = data.get("runtime_id")
        endpoint = data.get("endpoint")
        meta = data.get("meta")
        if not isinstance(runtime_id, str) or not runtime_id.strip():
            raise ValueError("hosting reference is missing runtime_id")
        if not isinstance(endpoint, str) or not endpoint.strip():
            raise ValueError("hosting reference is missing endpoint")
        return cls(
            runtime_id=runtime_id.strip(),
            endpoint=endpoint.strip(),
            meta=(
                {str(key): value for key, value in meta.items()}
                if isinstance(meta, dict)
                else {}
            ),
        )
