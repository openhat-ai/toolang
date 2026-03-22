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

    def spec(self) -> str:
        """Return the canonical sandbox spec string for persisted state."""

        if self.type == "docker" and self.image_name:
            return f"docker:{self.image_name}"
        return self.type or HOST_SANDBOX
