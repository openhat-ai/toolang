"""Shared Toolang error types exposed to plugins."""

from __future__ import annotations

from .types.sandbox import SandboxRef


class ToolangError(Exception):
    """Raised when Toolang input, configuration, or runtime behavior is invalid."""


class SandboxLaunchError(ToolangError):
    """Report a failed launch whose workload may still require recovery."""

    def __init__(self, message: str, *, ref: SandboxRef) -> None:
        super().__init__(message)
        self.ref = ref
