"""OpenAI model provider."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from toolang.base.error import ToolangError
from toolang.base.protocols.model import ModelProvider
from toolang.base.types.message import AudioPart
from toolang.base.types.model import ModelInfo, ModelTarget
from toolang.base.types.run import ModelCall, ModelCallResult, ModelEventHandler
from ..adapters import responses


_DEFAULT_BASE_URL = "https://api.openai.com/v1"
_DEFAULT_API_KEY_ENV = "OPENAI_API_KEY"
_OPENAI_ADAPTER = "responses"
_AUDIO_MODEL_PREFIXES: tuple[str, ...] = (
    "gpt-audio",
    "gpt-4o-audio-preview",
    "gpt-4o-mini-audio-preview",
)


@dataclass(frozen=True, slots=True)
class _KnownOpenAIModel:
    name: str
    context_window: int
    max_output_tokens: int
    input_price: float
    output_price: float
    tools: bool = True
    streaming: bool = True

    @property
    def ref(self) -> str:
        return f"openai/{self.name}"

    @property
    def input_price_per_token(self) -> float:
        return self.input_price / 1_000_000

    @property
    def output_price_per_token(self) -> float:
        return self.output_price / 1_000_000


_KNOWN_MODELS: tuple[_KnownOpenAIModel, ...] = (
    _KnownOpenAIModel("gpt-5.5", 1_050_000, 128_000, 5.00, 30.00),
    _KnownOpenAIModel("gpt-5.5-pro", 1_050_000, 128_000, 30.00, 180.00),
    _KnownOpenAIModel("gpt-5.4", 1_050_000, 128_000, 2.50, 15.00),
    _KnownOpenAIModel("gpt-5.4-mini", 400_000, 128_000, 0.75, 4.50),
    _KnownOpenAIModel("gpt-5.4-nano", 400_000, 128_000, 0.20, 1.25),
    _KnownOpenAIModel("gpt-5.4-pro", 1_050_000, 128_000, 30.00, 180.00),
    _KnownOpenAIModel("gpt-5.3-codex", 400_000, 128_000, 1.75, 14.00),
    _KnownOpenAIModel("gpt-5.3-chat-latest", 128_000, 16_384, 1.75, 14.00),
    _KnownOpenAIModel("gpt-5.2", 400_000, 128_000, 1.75, 14.00),
    _KnownOpenAIModel("gpt-5.2-pro", 400_000, 128_000, 21.00, 168.00),
    _KnownOpenAIModel("gpt-5.2-codex", 400_000, 128_000, 1.75, 14.00),
    _KnownOpenAIModel("gpt-5.2-chat-latest", 128_000, 16_384, 1.75, 14.00),
    _KnownOpenAIModel("gpt-5.1", 400_000, 128_000, 1.25, 10.00),
    _KnownOpenAIModel("gpt-5.1-codex", 400_000, 128_000, 1.25, 10.00),
    _KnownOpenAIModel("gpt-5.1-chat-latest", 128_000, 16_384, 1.25, 10.00),
    _KnownOpenAIModel("gpt-5", 400_000, 128_000, 1.25, 10.00),
    _KnownOpenAIModel("gpt-5-codex", 400_000, 128_000, 1.25, 10.00),
    _KnownOpenAIModel("gpt-5-mini", 400_000, 128_000, 0.25, 2.00),
    _KnownOpenAIModel("gpt-5-nano", 400_000, 128_000, 0.05, 0.40),
    _KnownOpenAIModel("gpt-5-pro", 400_000, 272_000, 15.00, 120.00),
    _KnownOpenAIModel("o3", 200_000, 100_000, 2.00, 8.00),
    _KnownOpenAIModel("o4-mini", 200_000, 100_000, 1.10, 4.40),
    _KnownOpenAIModel("o3-mini", 200_000, 100_000, 1.10, 4.40),
    _KnownOpenAIModel("gpt-4.1", 1_047_576, 32_768, 2.00, 8.00),
    _KnownOpenAIModel("gpt-4.1-mini", 1_047_576, 32_768, 0.40, 1.60),
    _KnownOpenAIModel("gpt-4.1-nano", 1_047_576, 32_768, 0.10, 0.40),
    _KnownOpenAIModel("gpt-4o", 128_000, 16_384, 2.50, 10.00),
    _KnownOpenAIModel("gpt-4o-mini", 128_000, 16_384, 0.15, 0.60),
)


@dataclass(frozen=True, slots=True)
class OpenAIModelProvider(ModelProvider):
    """OpenAI-backed model integration."""

    name: str = "openai"
    description: str | None = "Use OpenAI-hosted models."
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
                selectors=(model.name, model.ref),
                adapter=_OPENAI_ADAPTER,
                tools=model.tools,
                streaming=model.streaming,
                context_window=model.context_window,
                max_output_tokens=model.max_output_tokens,
                input_price=model.input_price_per_token,
                output_price=model.output_price_per_token,
                details="Built-in OpenAI model.",
            )
            for model in _KNOWN_MODELS
        )

    def invoke(
        self,
        target: ModelTarget,
        request: ModelCall,
    ) -> ModelCallResult:
        if target.adapter != _OPENAI_ADAPTER:
            raise ToolangError(f"unsupported openai adapter: {target.adapter}")
        _require_supported_openai_inputs(target, request)
        return responses.invoke_response(
            target,
            request,
            stateful=True,
        )

    def stream(
        self,
        target: ModelTarget,
        request: ModelCall,
        *,
        on_event: ModelEventHandler,
    ) -> ModelCallResult:
        if target.adapter != _OPENAI_ADAPTER:
            raise ToolangError(f"unsupported openai adapter: {target.adapter}")
        _require_supported_openai_inputs(target, request)
        return responses.stream_response(
            target,
            request,
            stateful=True,
            on_event=on_event,
        )


def _require_supported_openai_inputs(
    target: ModelTarget,
    request: ModelCall,
) -> None:
    if _supports_openai_audio_input(target):
        return
    if not _request_has_audio_input(request):
        return
    raise ToolangError(
        f"audio input is not supported for OpenAI model '{target.model}' via the Responses adapter; "
        "transcribe audio first, send it as a generic file to a route that supports audio files, or use an audio-capable model route"
    )


def _supports_openai_audio_input(target: ModelTarget) -> bool:
    candidates = (
        target.model.strip().lower(),
        target.ref.strip().lower(),
        target.name.strip().lower(),
    )
    return any(
        candidate.startswith(prefix)
        for candidate in candidates
        for prefix in _AUDIO_MODEL_PREFIXES
    )


def _request_has_audio_input(request: ModelCall) -> bool:
    return any(
        isinstance(part, AudioPart)
        for message in request.messages
        for part in message.parts
    )


def create_model(config: Mapping[str, object]) -> ModelProvider:
    """Create the built-in OpenAI model provider."""

    return OpenAIModelProvider(
        base_url=_config_str(config, "endpoint") or _DEFAULT_BASE_URL,
        key_env=_config_str(config, "key_env") or _DEFAULT_API_KEY_ENV,
    )


def _config_str(config: Mapping[str, object], key: str) -> str | None:
    value = config.get(key)
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None
