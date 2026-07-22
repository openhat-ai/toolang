"""Shared model provider and adapter protocols."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol, runtime_checkable

from ..events import ModelEventHandler
from ..types.model import ModelInfo, ModelTarget
from ..types.run import ModelCall, ModelCallResult


@runtime_checkable
class ModelProvider(Protocol):
    """Minimal model provider contract."""

    name: str
    description: str | None

    def required_env_vars(self) -> tuple[str, ...]:
        """Return required environment variables for this provider."""

    def default_base_url(self, *, environ: Mapping[str, str]) -> str | None:
        """Return the default API base URL for this provider when known."""

    def default_api_key_env(self) -> str | None:
        """Return the default API key environment variable name when known."""

    def list_models(self, *, environ: Mapping[str, str]) -> tuple[ModelInfo, ...]:
        """Return model infos exposed by this provider."""

    def prepare_target(self, target: ModelTarget) -> ModelTarget:
        """Return a provider-adjusted model target before adapter execution."""

        return target


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
