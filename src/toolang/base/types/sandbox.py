"""Shared sandbox value types."""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from pathlib import Path
from typing import Any, Literal, TypeAlias


SandboxLocation: TypeAlias = Literal["host", "guest"]
SandboxOutput: TypeAlias = Literal["inherit", "file"]
JsonValue: TypeAlias = (
    str | int | float | bool | None | list["JsonValue"] | dict[str, "JsonValue"]
)


@dataclass(frozen=True, slots=True)
class SandboxMount:
    """Map one controller-host path into a workload environment path."""

    local_path: Path
    hosted_path: Path
    read_only: bool = False


@dataclass(frozen=True, slots=True)
class SandboxPort:
    """Publish one workload port on the controller host."""

    bind_host: str
    local_port: int
    hosted_port: int


@dataclass(frozen=True, slots=True)
class SandboxRequest:
    """Explicit inputs from which one sandbox prepares a launch.

    ``local_*`` paths belong to the controller host. ``hosted_*`` paths are the
    corresponding paths in the workload environment, which may be the host or a
    guest selected by the sandbox plugin.
    """

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
    output: SandboxOutput
    log_path: Path | None
    envs: dict[str, str] = field(default_factory=dict)
    mounts: tuple[SandboxMount, ...] = ()
    local_dev_artifact: Path | None = None
    dotenv_envs: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _validate_output(self.output, self.log_path)


@dataclass(frozen=True, slots=True)
class SandboxPlan:
    """One implementation-owned, fully prepared workload launch."""

    sandbox: str
    command: tuple[str, ...]
    working_directory: Path
    output: SandboxOutput
    log_path: Path | None
    endpoint: str
    envs: dict[str, str] = field(default_factory=dict)
    mounts: tuple[SandboxMount, ...] = ()
    ports: tuple[SandboxPort, ...] = ()
    meta: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _validate_output(self.output, self.log_path)


@dataclass(frozen=True, slots=True)
class SandboxRef:
    """Serializable reference to one launched workload."""

    runtime_id: str
    endpoint: str
    meta: dict[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        runtime_id = self.runtime_id.strip()
        endpoint = self.endpoint.strip()
        if not runtime_id:
            raise ValueError("sandbox reference requires runtime_id")
        if not endpoint:
            raise ValueError("sandbox reference requires endpoint")
        object.__setattr__(self, "runtime_id", runtime_id)
        object.__setattr__(self, "endpoint", endpoint)
        object.__setattr__(self, "meta", _json_object(self.meta))

    def to_data(self) -> dict[str, JsonValue]:
        return {
            "runtime_id": self.runtime_id,
            "endpoint": self.endpoint,
            "meta": dict(self.meta),
        }

    @classmethod
    def from_data(cls, payload: object) -> SandboxRef:
        if not isinstance(payload, dict):
            raise ValueError("sandbox reference must be a mapping")
        data = {str(key): value for key, value in payload.items()}
        runtime_id = data.get("runtime_id")
        endpoint = data.get("endpoint")
        meta = data.get("meta")
        if not isinstance(runtime_id, str) or not runtime_id.strip():
            raise ValueError("sandbox reference is missing runtime_id")
        if not isinstance(endpoint, str) or not endpoint.strip():
            raise ValueError("sandbox reference is missing endpoint")
        if meta is None:
            normalized_meta: dict[str, JsonValue] = {}
        elif isinstance(meta, dict):
            normalized_meta = _json_object(meta)
        else:
            raise ValueError("sandbox reference meta must be a mapping")
        return cls(
            runtime_id=runtime_id.strip(),
            endpoint=endpoint.strip(),
            meta=normalized_meta,
        )


def _json_object(value: object) -> dict[str, JsonValue]:
    if not isinstance(value, dict):
        raise ValueError("sandbox reference meta must be a mapping")
    normalized: dict[str, JsonValue] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise ValueError("sandbox reference meta keys must be strings")
        normalized[key] = _json_value(item)
    return normalized


def _validate_output(output: SandboxOutput, log_path: Path | None) -> None:
    if output == "inherit":
        if log_path is not None:
            raise ValueError("inherited sandbox output does not accept a log path")
        return
    if output == "file":
        if log_path is None:
            raise ValueError("sandbox file output requires a log path")
        return
    raise ValueError(f"unsupported sandbox output mode: {output}")


def _json_value(value: object) -> JsonValue:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("sandbox reference meta numbers must be finite")
        return value
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return _json_object(value)
    raise ValueError(
        f"sandbox reference meta values must be JSON-compatible: {type(value).__name__}"
    )
