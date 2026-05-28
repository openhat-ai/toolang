"""Google Gemini model provider."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from toolang.base.protocols.model import ModelProvider
from toolang.base.types.model import ModelInfo

_DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"
_DEFAULT_API_KEY_ENV = "GEMINI_API_KEY"
_GOOGLE_ADAPTER = "chat_completions"


@dataclass(frozen=True, slots=True)
class _KnownGoogleModel:
    name: str
    context_window: int
    max_output_tokens: int
    details: str
    tools: bool = True
    streaming: bool = True

    @property
    def ref(self) -> str:
        return f"google/{self.name}"


_KNOWN_MODELS: tuple[_KnownGoogleModel, ...] = (
    _KnownGoogleModel(
        "gemini-3.5-flash",
        1_048_576,
        65_536,
        "Built-in Google Gemini 3.5 Flash model.",
    ),
    _KnownGoogleModel(
        "gemini-3.1-pro-preview",
        1_048_576,
        65_536,
        "Built-in Google Gemini 3.1 Pro Preview model.",
    ),
    _KnownGoogleModel(
        "gemini-3-flash-preview",
        1_048_576,
        65_536,
        "Built-in Google Gemini 3 Flash Preview model.",
    ),
    _KnownGoogleModel(
        "gemini-3.1-flash-lite",
        1_048_576,
        65_536,
        "Built-in Google Gemini 3.1 Flash-Lite model.",
    ),
    _KnownGoogleModel(
        "gemini-2.5-pro",
        1_048_576,
        65_536,
        "Built-in Google Gemini 2.5 Pro model.",
    ),
    _KnownGoogleModel(
        "gemini-2.5-flash",
        1_048_576,
        65_536,
        "Built-in Google Gemini 2.5 Flash model.",
    ),
    _KnownGoogleModel(
        "gemini-2.5-flash-lite",
        1_048_576,
        65_536,
        "Built-in Google Gemini 2.5 Flash-Lite model.",
    ),
)


@dataclass(frozen=True, slots=True)
class GoogleModelProvider(ModelProvider):
    """Google Gemini-backed model integration."""

    name: str = "google"
    description: str | None = "Use Google-hosted Gemini models."
    base_url: str = _DEFAULT_BASE_URL
    key_env: str = _DEFAULT_API_KEY_ENV

    def required_env_vars(self) -> tuple[str, ...]:
        return (self.key_env,)

    def default_base_url(self, *, environ: Mapping[str, str]) -> str | None:
        del environ
        return self.base_url

    def default_api_key_env(self) -> str | None:
        return self.key_env

    def list_models(self, *, environ: Mapping[str, str]) -> tuple[ModelInfo, ...]:
        del environ
        return tuple(
            ModelInfo(
                ref=model.ref,
                provider=self.name,
                name=model.name,
                model=model.name,
                selectors=_selectors(model),
                adapter=_GOOGLE_ADAPTER,
                tools=model.tools,
                streaming=model.streaming,
                context_window=model.context_window,
                max_output_tokens=model.max_output_tokens,
                details=model.details,
            )
            for model in _KNOWN_MODELS
        )


def _selectors(model: _KnownGoogleModel) -> tuple[str, ...]:
    return (model.name, model.ref)


def create_model_provider(config: Mapping[str, object]) -> ModelProvider:
    """Create the built-in Google Gemini model provider."""

    return GoogleModelProvider(
        base_url=_config_str(config, "endpoint") or _DEFAULT_BASE_URL,
        key_env=_config_str(config, "key_env") or _DEFAULT_API_KEY_ENV,
    )


def _config_str(config: Mapping[str, object], key: str) -> str | None:
    value = config.get(key)
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None
