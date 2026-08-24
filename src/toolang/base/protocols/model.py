"""Shared model catalog and adapter protocols."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..types.model import ModelCatalogSnapshot, ModelTarget
from ..types.run import ModelCall, ModelCallResult, ModelStreamHandler


@runtime_checkable
class ModelCatalog(Protocol):
    """Source of one immutable model catalog snapshot."""

    name: str

    async def snapshot(self) -> ModelCatalogSnapshot:
        """Return the source's current immutable snapshot."""


@runtime_checkable
class ModelAdapter(Protocol):
    """Minimal model adapter contract."""

    name: str
    description: str | None
    default_endpoint: str | None

    async def invoke(
        self,
        target: ModelTarget,
        request: ModelCall,
    ) -> ModelCallResult:
        """Execute one non-streaming model turn."""

    async def stream(
        self,
        target: ModelTarget,
        request: ModelCall,
        *,
        on_event: ModelStreamHandler,
    ) -> ModelCallResult:
        """Execute one streaming model turn."""
