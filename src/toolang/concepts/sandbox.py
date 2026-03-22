"""Sandbox concepts shared by runtime and persisted state."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel

HOST_SANDBOX = "host"
SandboxKind = Literal["host", "docker"]


@dataclass(frozen=True, slots=True)
class SandboxSpec:
    """A normalized sandbox selection."""

    kind: SandboxKind
    image: str | None = None

    @classmethod
    def parse(cls, value: str | None, *, fallback: str = HOST_SANDBOX) -> "SandboxSpec":
        """Parse one sandbox selector into a normalized sandbox spec."""

        raw = (value or fallback).strip()
        if not raw or raw == "none":
            raw = HOST_SANDBOX
        if raw == HOST_SANDBOX:
            return cls(kind="host")
        if not raw.startswith("docker:"):
            raise ValueError("unsupported sandbox value; use 'host' or 'docker:<image>'")
        image = raw.split(":", 1)[1].strip()
        if not image:
            raise ValueError("docker sandbox must include an image")
        return cls(kind="docker", image=image)

    @property
    def spec(self) -> str:
        """Return the canonical sandbox spec string."""

        if self.kind == "docker" and self.image:
            return f"docker:{self.image}"
        return HOST_SANDBOX

    @property
    def execution_host(self) -> str:
        """Return the execution host category for this sandbox."""

        if self.kind == "docker":
            return "docker"
        return "local"


class SandboxRuntimeInfo(BaseModel):
    """Runtime details reported by the active sandbox process."""

    pid: int | None = None
    port: int | None = None


class SandboxState(BaseModel):
    """Persisted sandbox identity and runtime data."""

    type: SandboxKind = "host"
    container_name: str | None = None
    image_name: str | None = None
    run: SandboxRuntimeInfo | None = None

    @property
    def spec(self) -> str:
        """Return the canonical sandbox spec string for persisted state."""

        if self.type == "docker" and self.image_name:
            return f"docker:{self.image_name}"
        return self.type or HOST_SANDBOX

    @classmethod
    def for_spec(
        cls,
        spec: SandboxSpec,
        *,
        agent_name: str,
        agent_id: str,
        pid: int | None = None,
        port: int | None = None,
    ) -> "SandboxState":
        """Build one persisted sandbox state from a spec and runtime details."""

        runtime = SandboxRuntimeInfo(pid=pid, port=port)
        if spec.kind == "docker":
            return cls(
                type="docker",
                container_name=_docker_container_name(agent_name, agent_id),
                image_name=spec.image,
                run=runtime,
            )
        return cls(type="host", run=runtime)


def _docker_container_name(agent_name: str, agent_id: str) -> str:
    return f"toolang-agent-{agent_name}-{agent_id[:12]}"
