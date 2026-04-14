"""Shared model protocols."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol, runtime_checkable

from ..types.model import (
    ModelCapabilities,
    ResolvedModel,
)
from ..types.run import ModelCall, ModelCallResult, ModelEventHandler


@runtime_checkable
class ModelPlugin(Protocol):
    """Minimal model plugin contract."""

    name: str
    description: str | None

    def capabilities(self) -> ModelCapabilities:
        """Return one stable capability snapshot."""

    def resolve_selector(
        self,
        selector: str,
        *,
        environ: Mapping[str, str],
    ) -> ResolvedModel | None:
        """Resolve one selector without named profile config."""

    def invoke(
        self,
        target: ResolvedModel,
        request: ModelCall,
    ) -> ModelCallResult:
        """Execute one non-streaming model turn."""

    def stream(
        self,
        target: ResolvedModel,
        request: ModelCall,
        *,
        on_event: ModelEventHandler,
    ) -> ModelCallResult:
        """Execute one streaming model turn."""
