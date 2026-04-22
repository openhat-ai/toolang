"""OpenAI model provider."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from toolang.base.error import ToolangError
from toolang.base.protocols.model import ModelProvider
from toolang.base.types.message import AudioPart
from toolang.base.types.model import ModelInfo, ModelTarget
from toolang.base.types.run import ModelCall, ModelCallResult, ModelEventHandler
from . import responses


_KNOWN_MODELS: tuple[tuple[str, str], ...] = (
    ("gpt-5", "openai/gpt-5"),
    ("o3", "openai/o3"),
)
_DEFAULT_BASE_URL = "https://api.openai.com/v1"
_DEFAULT_API_KEY_ENV = "OPENAI_API_KEY"
_OPENAI_ADAPTER = "responses"
_AUDIO_MODEL_PREFIXES: tuple[str, ...] = (
    "gpt-audio",
    "gpt-4o-audio-preview",
    "gpt-4o-mini-audio-preview",
)


@dataclass(frozen=True, slots=True)
class OpenAIModelProvider(ModelProvider):
    """OpenAI-backed model integration."""

    name: str = "openai"
    description: str | None = "Use OpenAI-hosted models."

    def required_env_vars(self) -> tuple[str, ...]:
        return (_DEFAULT_API_KEY_ENV,)

    def default_base_url(self, *, environ: Mapping[str, str]) -> str | None:
        del environ
        return _DEFAULT_BASE_URL

    def default_api_key_env(self) -> str | None:
        return _DEFAULT_API_KEY_ENV

    def list_models(self, *, environ: Mapping[str, str]) -> tuple[ModelInfo, ...]:
        del environ
        return tuple(
            ModelInfo(
                ref=ref,
                provider=self.name,
                name=name,
                model=name,
                selectors=(name, ref),
                adapter=_OPENAI_ADAPTER,
                tools=True,
                streaming=True,
                details="Built-in OpenAI route. Also accepts selectors beginning with gpt- or o-.",
            )
            for name, ref in _KNOWN_MODELS
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

    del config
    return OpenAIModelProvider()
