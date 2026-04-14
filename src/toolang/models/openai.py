"""OpenAI model plugin."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from toolang.base.protocols.model import ModelPlugin
from toolang.base.types.model import ModelCapabilities, ResolvedModel
from toolang.base.types.run import ModelCall, ModelCallResult, ModelEventHandler
from . import _openai_compat


_SHORTHAND_PREFIXES = ("gpt-", "o")


@dataclass(frozen=True, slots=True)
class OpenAIModelPlugin(ModelPlugin):
    """OpenAI-backed model integration."""

    name: str = "openai"
    description: str | None = "Use OpenAI-hosted models."

    def capabilities(self) -> ModelCapabilities:
        return ModelCapabilities(
            tools=True,
            streaming=True,
        )

    def resolve_selector(
        self,
        selector: str,
        *,
        environ: Mapping[str, str],
    ) -> ResolvedModel | None:
        text = selector.strip()
        if not text:
            return None
        if text.startswith("openai/"):
            model_name = text.partition("/")[2].strip()
            if not model_name:
                return None
            return ResolvedModel(
                ref=text,
                plugin=self.name,
                model=model_name,
                api_key=environ.get("OPENAI_API_KEY"),
            )
        if text.startswith(_SHORTHAND_PREFIXES):
            return ResolvedModel(
                ref=f"openai/{text}",
                plugin=self.name,
                model=text,
                api_key=environ.get("OPENAI_API_KEY"),
            )
        return None

    def invoke(
        self,
        target: ResolvedModel,
        request: ModelCall,
    ) -> ModelCallResult:
        return _openai_compat.invoke_response(
            target,
            request,
            stateful=True,
        )

    def stream(
        self,
        target: ResolvedModel,
        request: ModelCall,
        *,
        on_event: ModelEventHandler,
    ) -> ModelCallResult:
        return _openai_compat.stream_response(
            target,
            request,
            stateful=True,
            on_event=on_event,
        )


def create_model(config: Mapping[str, object]) -> ModelPlugin:
    """Create the built-in OpenAI model plugin."""

    del config
    return OpenAIModelPlugin()
