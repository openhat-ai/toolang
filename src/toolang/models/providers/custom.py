"""Custom model alias provider."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from toolang.base.protocols.model import ModelProvider
from toolang.base.types.model import ModelInfo


@dataclass(frozen=True, slots=True)
class CustomModelProvider(ModelProvider):
    """Alias-only provider for user-defined OpenAI-compatible endpoints."""

    name: str = "custom"
    description: str | None = "Use user-defined model aliases."

    def required_env_vars(self) -> tuple[str, ...]:
        return ()

    def default_base_url(self, *, environ: Mapping[str, str]) -> str | None:
        del environ
        return None

    def default_api_key_env(self) -> str | None:
        return None

    def list_models(self, *, environ: Mapping[str, str]) -> tuple[ModelInfo, ...]:
        del environ
        return ()

def create_model_provider(config: Mapping[str, object]) -> ModelProvider:
    """Create the built-in custom alias provider."""

    del config
    return CustomModelProvider()
