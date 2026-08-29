"""Shared sandbox protocol."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from ..types.progress import ProgressSink
from ..types.sandbox import (
    SandboxLocation,
    SandboxPlan,
    SandboxRef,
    SandboxRequest,
)


@runtime_checkable
class Sandbox(Protocol):
    """Lifecycle operations implemented by one sandbox plugin."""

    name: str
    location: SandboxLocation

    def runtime_root(self, local_root: Path) -> Path:
        """Resolve the Toolang root used by the workload environment."""

    def prepare(self, spec: str | None, request: SandboxRequest) -> SandboxPlan:
        """Parse an implementation-owned spec and prepare one launch."""

    async def launch(self, plan: SandboxPlan) -> SandboxRef:
        """Launch one workload and return its durable recovery reference."""

    async def attach(
        self,
        plan: SandboxPlan,
        ref: SandboxRef,
        *,
        progress: ProgressSink | None = None,
        progress_id: str | None = None,
    ) -> None:
        """Attach process-local observers after the reference is persisted."""

    async def running(self, ref: SandboxRef) -> bool:
        """Return whether a launched workload is still running."""

    async def wait(self, ref: SandboxRef) -> int:
        """Wait for a workload to exit and return its exit code."""

    async def stop(self, ref: SandboxRef, *, force: bool = False) -> None:
        """Stop the primary workload."""

    async def release(self, ref: SandboxRef) -> None:
        """Release resources created for a workload."""
