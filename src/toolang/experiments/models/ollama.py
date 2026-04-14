"""Ollama model plugin."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from ..base.protocols.model import ModelPlugin
from ..base.types.model import ModelCapabilities, ResolvedModel
from ..base.types.run import ModelCall, ModelCallResult, ModelEventHandler
from . import _openai_compat

_NAMESPACE_BY_PREFIX: tuple[tuple[str, str], ...] = (
    ("qwen", "qwen"),
    ("llama", "meta"),
    ("deepseek", "deepseek"),
    ("mistral", "mistral"),
    ("gemma", "google"),
)
_CANONICAL_NAMESPACES = frozenset(
    {"qwen", "meta", "deepseek", "mistral", "google", "moonshot", "nomic", "allenai"}
)


@dataclass(frozen=True, slots=True)
class OllamaModelPlugin(ModelPlugin):
    """Ollama-backed local model integration."""

    name: str = "ollama"
    description: str | None = "Use local Ollama-hosted models."

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
        ref, model_name = _canonical_ollama_ref(text)
        if ref is None or model_name is None:
            return None
        return ResolvedModel(
            ref=ref,
            plugin=self.name,
            model=model_name,
            base_url=_ollama_base_url(environ),
            api_key=environ.get("OLLAMA_API_KEY", "ollama"),
        )

    def invoke(
        self,
        target: ResolvedModel,
        request: ModelCall,
    ) -> ModelCallResult:
        return _openai_compat.invoke_response(
            target,
            request,
            stateful=False,
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
            stateful=False,
            on_event=on_event,
        )


def _canonical_ollama_ref(selector: str) -> tuple[str | None, str | None]:
    if "/" in selector:
        namespace, _, model_name = selector.partition("/")
        namespace = namespace.strip()
        model_name = model_name.strip()
        if namespace in _CANONICAL_NAMESPACES and model_name:
            return f"{namespace}/{model_name}", model_name
        return None, None
    for prefix, namespace in _NAMESPACE_BY_PREFIX:
        if selector.startswith(prefix):
            return f"{namespace}/{selector}", selector
    return None, None


def _ollama_base_url(environ: Mapping[str, str]) -> str:
    host = environ.get("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
    return f"{host}/v1"


def create_model(config: Mapping[str, object]) -> ModelPlugin:
    """Create the built-in Ollama model plugin."""

    del config
    return OllamaModelPlugin()
