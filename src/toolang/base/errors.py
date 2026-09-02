"""Shared Toolang error types exposed to plugins."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .types.sandbox import SandboxRef


class ToolangError(Exception):
    """Raised when Toolang input, configuration, or runtime behavior is invalid."""


class ToolFailure(ToolangError):
    """Report an expected failed tool call with structured model-facing output."""

    def __init__(self, message: str, *, output: Mapping[str, Any]) -> None:
        super().__init__(message)
        self.output = dict(output)


class SandboxLaunchError(ToolangError):
    """Report a failed launch whose workload may still require recovery."""

    def __init__(self, message: str, *, ref: SandboxRef) -> None:
        super().__init__(message)
        self.ref = ref
