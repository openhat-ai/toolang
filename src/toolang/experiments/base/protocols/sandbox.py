"""Shared sandbox protocols."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..types.sandbox import (
    SandboxPlan,
    SandboxSelector,
    SandboxStartRequest,
    SandboxStartResult,
    SandboxState,
)


@runtime_checkable
class SandboxPlugin(Protocol):
    """Protocol implemented by one sandbox plugin instance."""

    name: str

    def resolve_selector(
        self,
        raw_selector: str | None,
        *,
        configured_selector: SandboxSelector | None = None,
    ) -> SandboxSelector:
        """Resolve one explicit or configured selector for this sandbox."""

    def prepare(self, request: SandboxStartRequest) -> SandboxPlan:
        """Prepare one launch plan from explicit local runtime inputs."""

    def start(self, plan: SandboxPlan) -> SandboxStartResult:
        """Start one prepared plan inside the sandbox and return its runtime state."""

    def alive(self, state: SandboxState) -> bool:
        """Return whether one previously started sandbox still appears alive."""

    def stop(self, state: SandboxState, *, force: bool = False) -> None:
        """Stop one previously started sandbox."""
