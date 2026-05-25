"""Shared model adapter protocols."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..types.model import ModelTarget
from ..types.run import ModelCall, ModelCallResult, ModelEventHandler


@runtime_checkable
class ModelAdapter(Protocol):
    """Minimal model adapter contract."""

    name: str
    description: str | None

    def invoke(
        self,
        target: ModelTarget,
        request: ModelCall,
    ) -> ModelCallResult:
        """Execute one non-streaming model turn."""

    def stream(
        self,
        target: ModelTarget,
        request: ModelCall,
        *,
        on_event: ModelEventHandler,
    ) -> ModelCallResult:
        """Execute one streaming model turn."""
