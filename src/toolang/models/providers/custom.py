"""Custom model alias provider."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from toolang.base.error import ToolangError
from toolang.base.protocols.model import ModelProvider
from toolang.base.types.model import ModelInfo, ModelTarget
from toolang.base.types.run import ModelCall, ModelCallResult, ModelEventHandler
from ..adapters import responses


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

    def invoke(
        self,
        target: ModelTarget,
        request: ModelCall,
    ) -> ModelCallResult:
        if target.adapter != "responses":
            raise ToolangError(f"unsupported custom adapter: {target.adapter}")
        return responses.invoke_response(target, request, stateful=False)

    def stream(
        self,
        target: ModelTarget,
        request: ModelCall,
        *,
        on_event: ModelEventHandler,
    ) -> ModelCallResult:
        if target.adapter != "responses":
            raise ToolangError(f"unsupported custom adapter: {target.adapter}")
        return responses.stream_response(
            target,
            request,
            stateful=False,
            on_event=on_event,
        )


def create_model(config: Mapping[str, object]) -> ModelProvider:
    """Create the built-in custom alias provider."""

    del config
    return CustomModelProvider()
