"""DeepSeek model provider."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from toolang.base.protocols.model import ModelProvider
from toolang.base.types.model import ModelInfo

_DEFAULT_BASE_URL = "https://api.deepseek.com"
_DEFAULT_API_KEY_ENV = "DEEPSEEK_API_KEY"
_DEEPSEEK_ADAPTER = "chat_completions"


@dataclass(frozen=True, slots=True)
class _KnownDeepSeekModel:
    name: str
    context_window: int
    max_output_tokens: int
    input_price: float
    output_price: float
    details: str
    tools: bool = True
    streaming: bool = True

    @property
    def ref(self) -> str:
        return f"deepseek/{self.name}"

    @property
    def input_price_per_token(self) -> float:
        return self.input_price / 1_000_000

    @property
    def output_price_per_token(self) -> float:
        return self.output_price / 1_000_000


_KNOWN_MODELS: tuple[_KnownDeepSeekModel, ...] = (
    _KnownDeepSeekModel(
        "deepseek-v4-flash",
        1_000_000,
        384_000,
        0.14,
        0.28,
        "Built-in DeepSeek V4 Flash model.",
    ),
    _KnownDeepSeekModel(
        "deepseek-v4-pro",
        1_000_000,
        384_000,
        0.435,
        0.87,
        "Built-in DeepSeek V4 Pro model.",
    ),
    _KnownDeepSeekModel(
        "deepseek-chat",
        1_000_000,
        384_000,
        0.14,
        0.28,
        "Deprecated DeepSeek compatibility model for non-thinking V4 Flash.",
    ),
    _KnownDeepSeekModel(
        "deepseek-reasoner",
        1_000_000,
        384_000,
        0.14,
        0.28,
        "Deprecated DeepSeek compatibility model for thinking V4 Flash.",
        tools=False,
    ),
)


@dataclass(frozen=True, slots=True)
class DeepSeekModelProvider(ModelProvider):
    """DeepSeek-backed model integration."""

    name: str = "deepseek"
    description: str | None = "Use DeepSeek-hosted models."
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
                adapter=_DEEPSEEK_ADAPTER,
                tools=model.tools,
                streaming=model.streaming,
                context_window=model.context_window,
                max_output_tokens=model.max_output_tokens,
                input_price=model.input_price_per_token,
                output_price=model.output_price_per_token,
                details=model.details,
            )
            for model in _KNOWN_MODELS
        )


def _selectors(model: _KnownDeepSeekModel) -> tuple[str, ...]:
    return (model.name, model.ref)


def create_model_provider(config: Mapping[str, object]) -> ModelProvider:
    """Create the built-in DeepSeek model provider."""

    return DeepSeekModelProvider(
        base_url=_config_str(config, "endpoint") or _DEFAULT_BASE_URL,
        key_env=_config_str(config, "key_env") or _DEFAULT_API_KEY_ENV,
    )


def _config_str(config: Mapping[str, object], key: str) -> str | None:
    value = config.get(key)
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None
