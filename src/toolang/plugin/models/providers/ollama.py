"""Ollama model provider."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

import httpx

from toolang.base.protocols.model import ModelProvider
from toolang.base.types.model import ModelInfo

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
_OLLAMA_ADAPTER = "responses"


@dataclass(frozen=True, slots=True)
class OllamaModelProvider(ModelProvider):
    """Ollama-backed local model integration."""

    name: str = "ollama"
    description: str | None = "Use local Ollama-hosted models."
    base_url: str | None = None

    def required_env_vars(self) -> tuple[str, ...]:
        return ()

    def default_base_url(self, *, environ: Mapping[str, str]) -> str | None:
        if self.base_url is not None:
            return self.base_url
        return _ollama_base_url(environ)

    def default_api_key_env(self) -> str | None:
        return None

    def list_models(self, *, environ: Mapping[str, str]) -> tuple[ModelInfo, ...]:
        response = httpx.get(f"{_ollama_host(environ)}/api/tags", timeout=2.0)
        response.raise_for_status()
        payload = response.json()
        raw_models = payload.get("models")
        if not isinstance(raw_models, list):
            return ()
        discovered: list[ModelInfo] = []
        seen: set[str] = set()
        for item in raw_models:
            if not isinstance(item, dict):
                continue
            raw_name = item.get("model") or item.get("name")
            if not isinstance(raw_name, str):
                continue
            model_name = raw_name.strip()
            if not model_name or model_name in seen:
                continue
            ref, canonical_name = _canonical_ollama_ref(model_name)
            if ref is None or canonical_name is None:
                continue
            seen.add(model_name)
            tools, streaming = _ollama_capabilities(ref)
            discovered.append(
                ModelInfo(
                    ref=ref,
                    provider=self.name,
                    name=canonical_name,
                    model=canonical_name,
                    selectors=(canonical_name, ref),
                    adapter=_OLLAMA_ADAPTER,
                    tools=tools,
                    streaming=streaming,
                    details="Local Ollama model.",
                )
            )
        return tuple(sorted(discovered, key=lambda item: item.name))

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
    host = _ollama_host(environ)
    return f"{host}/v1"


def _ollama_host(environ: Mapping[str, str]) -> str:
    return environ.get("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")


def _ollama_capabilities(ref: str) -> tuple[bool, bool]:
    namespace = ref.partition("/")[0]
    if namespace == "google":
        return False, True
    if namespace == "qwen":
        return True, True
    return False, True


def create_model_provider(config: Mapping[str, object]) -> ModelProvider:
    """Create the built-in Ollama model provider."""

    return OllamaModelProvider(base_url=_config_str(config, "endpoint"))


def _config_str(config: Mapping[str, object], key: str) -> str | None:
    value = config.get(key)
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None
