"""Shared model catalog and adapter protocols."""

from __future__ import annotations

from collections.abc import Mapping
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
    default_api: str | None

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


@runtime_checkable
class InspectableModelAdapter(Protocol):
    """Optional adapter capability for provider request inspection."""

    def request_payload(
        self,
        target: ModelTarget,
        request: ModelCall,
    ) -> Mapping[str, object]:
        """Build the provider-native JSON body without network I/O."""
