"""Shared model catalog and adapter protocols."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from collections.abc import Mapping

from ..types.model import Model, ModelCatalogSnapshot
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
    default_api: str | None

    async def invoke(
        self,
        model: Model,
        request: ModelCall,
        *,
        environ: Mapping[str, str],
    ) -> ModelCallResult:
        """Execute one non-streaming model turn."""

    async def stream(
        self,
        model: Model,
        request: ModelCall,
        *,
        environ: Mapping[str, str],
        on_event: ModelStreamHandler,
    ) -> ModelCallResult:
        """Execute one streaming model turn."""
