"""Shared Toolang error types exposed to plugins."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from .types.sandbox import SandboxRef

if TYPE_CHECKING:
    from .types.run import ModelUsage


class ToolangError(Exception):
    """Raised when Toolang input, configuration, or runtime behavior is invalid."""


class ModelResponseError(ToolangError):
    """An unusable provider response, before any of its tool calls execute.

    Adapters retain available usage and partial text. The runtime owns recovery.
    """

    def __init__(
        self,
        message: str,
        *,
        kind: Literal[
            "output_limit",
            "incomplete_stream",
            "invalid_json",
            "non_object_arguments",
            "missing_name",
            "provider_rejection",
            "transport_error",
        ],
        usage: ModelUsage | None = None,
        partial_text: str = "",
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.usage = usage
        self.partial_text = partial_text
        self.retry_after = retry_after

    @property
    def recoverable(self) -> bool:
        return self.kind != "provider_rejection"


class SandboxLaunchError(ToolangError):
    """Report a failed launch whose workload may still require recovery."""

    def __init__(self, message: str, *, ref: SandboxRef) -> None:
        super().__init__(message)
        self.ref = ref
