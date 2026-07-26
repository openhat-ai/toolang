"""Shared hosting protocol."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..types.hosting import HostingPlan, HostingRef, HostingRequest


@runtime_checkable
class Hosting(Protocol):
    """Lifecycle operations implemented by one sandbox plugin."""

    name: str

    def prepare(self, spec: str | None, request: HostingRequest) -> HostingPlan:
        """Parse an implementation-owned spec and prepare one launch."""

    async def launch(self, plan: HostingPlan) -> HostingRef:
        """Launch one prepared workload."""

    async def running(self, ref: HostingRef) -> bool:
        """Return whether a launched workload is still running."""

    async def wait(self, ref: HostingRef) -> int:
        """Wait for a workload to exit and return its exit code."""

    async def stop(self, ref: HostingRef, *, force: bool = False) -> None:
        """Stop the primary workload."""

    async def release(self, ref: HostingRef) -> None:
        """Release resources created for a workload."""
