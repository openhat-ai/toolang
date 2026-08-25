"""Shared sandbox protocol."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..types.sandbox import SandboxPlan, SandboxRef, SandboxRequest


@runtime_checkable
class Sandbox(Protocol):
    """Lifecycle operations implemented by one sandbox plugin."""

    name: str

    def prepare(self, spec: str | None, request: SandboxRequest) -> SandboxPlan:
        """Parse an implementation-owned spec and prepare one launch."""

    async def launch(self, plan: SandboxPlan) -> SandboxRef:
        """Launch one prepared workload."""

    async def running(self, ref: SandboxRef) -> bool:
        """Return whether a launched workload is still running."""

    async def wait(self, ref: SandboxRef) -> int:
        """Wait for a workload to exit and return its exit code."""

    async def stop(self, ref: SandboxRef, *, force: bool = False) -> None:
        """Stop the primary workload."""

    async def release(self, ref: SandboxRef) -> None:
        """Release resources created for a workload."""
