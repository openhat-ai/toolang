"""OpenRouter model provider."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import cast

import httpx

from toolang.base.error import ToolangError
from toolang.base.protocols.model import ModelProvider
from toolang.base.types.model import ModelInfo, ModelTarget
from toolang.base.types.run import ModelCall, ModelCallResult, ModelEventHandler
from . import responses

_DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
_DEFAULT_API_KEY_ENV = "OPENROUTER_API_KEY"
_OPENROUTER_ADAPTER = "responses"
_APP_REFERER = "https://toolang.ai"
_APP_TITLE = "Toolang"
_APP_CATEGORIES = "cli-agent"


@dataclass(frozen=True, slots=True)
class OpenRouterModelProvider(ModelProvider):
    """OpenRouter-backed model integration."""

    name: str = "openrouter"
    description: str | None = "Use OpenRouter-hosted models."

    def required_env_vars(self) -> tuple[str, ...]:
        return (_DEFAULT_API_KEY_ENV,)

    def default_base_url(self, *, environ: Mapping[str, str]) -> str | None:
        del environ
        return _DEFAULT_BASE_URL

    def default_api_key_env(self) -> str | None:
        return _DEFAULT_API_KEY_ENV

    def list_models(self, *, environ: Mapping[str, str]) -> tuple[ModelInfo, ...]:
        api_key = environ.get(_DEFAULT_API_KEY_ENV, "").strip()
        if not api_key:
            return ()
        try:
            response = httpx.get(
                f"{_DEFAULT_BASE_URL}/models",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    **_app_attribution_headers(),
                },
                timeout=3.0,
            )
            response.raise_for_status()
            payload = response.json()
        except Exception:
            return ()
        raw_models = payload.get("data")
        if not isinstance(raw_models, list):
            return ()
        discovered: list[ModelInfo] = []
        seen: set[tuple[str, str]] = set()
        for item in raw_models:
            info = _parse_model_info(item)
            if info is None:
                continue
            key = (info.provider, info.ref)
            if key in seen:
                continue
            seen.add(key)
            discovered.append(info)
        return tuple(sorted(discovered, key=lambda item: (item.ref, item.name)))

    def invoke(
        self,
        target: ModelTarget,
        request: ModelCall,
    ) -> ModelCallResult:
        if target.adapter != _OPENROUTER_ADAPTER:
            raise ToolangError(f"unsupported openrouter adapter: {target.adapter}")
        return responses.invoke_response(
            _target_with_app_attribution(target),
            request,
            stateful=False,
        )

    def stream(
        self,
        target: ModelTarget,
        request: ModelCall,
        *,
        on_event: ModelEventHandler,
    ) -> ModelCallResult:
        if target.adapter != _OPENROUTER_ADAPTER:
            raise ToolangError(f"unsupported openrouter adapter: {target.adapter}")
        return responses.stream_response(
            _target_with_app_attribution(target),
            request,
            stateful=False,
            on_event=on_event,
        )


def _parse_model_info(item: object) -> ModelInfo | None:
    if not isinstance(item, dict):
        return None
    payload = cast(Mapping[str, object], item)
    raw_ref = payload.get("canonical_slug") or payload.get("id")
    if not isinstance(raw_ref, str):
        return None
    ref = raw_ref.strip()
    if not ref or "/" not in ref:
        return None
    raw_model = payload.get("id")
    model = raw_model.strip() if isinstance(raw_model, str) and raw_model.strip() else ref
    short_name = ref.rsplit("/", 1)[-1]
    selectors = _selectors(ref=ref, model=model, short_name=short_name)
    supported_parameters = _string_items(payload.get("supported_parameters"))
    pricing = payload.get("pricing")
    context_window = _int_or_none(payload.get("context_length"))
    max_output_tokens = _max_output_tokens(payload.get("top_provider"))
    details = payload.get("description")
    return ModelInfo(
        ref=ref,
        provider="openrouter",
        name=short_name,
        model=model,
        selectors=selectors,
        adapter=_OPENROUTER_ADAPTER,
        tools="tools" in supported_parameters or "tool_choice" in supported_parameters,
        streaming=True,
        context_window=context_window,
        max_output_tokens=max_output_tokens,
        input_price=_pricing_value(pricing, key="prompt"),
        output_price=_pricing_value(pricing, key="completion"),
        details=details.strip() if isinstance(details, str) and details.strip() else "Built-in OpenRouter route.",
        metadata={},
    )


def _selectors(*, ref: str, model: str, short_name: str) -> tuple[str, ...]:
    values = [short_name, ref]
    if model != ref:
        values.append(model)
    return tuple(_dedupe_non_empty(values))


def _dedupe_non_empty(items: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        value = item.strip()
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _string_items(value: object) -> set[str]:
    if not isinstance(value, list):
        return set()
    return {
        item.strip()
        for item in value
        if isinstance(item, str) and item.strip()
    }


def _pricing_value(pricing: object, *, key: str) -> float | None:
    if not isinstance(pricing, dict):
        return None
    payload = cast(Mapping[str, object], pricing)
    raw = payload.get(key)
    if not isinstance(raw, (str, int, float)):
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _int_or_none(value: object) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _max_output_tokens(top_provider: object) -> int | None:
    if not isinstance(top_provider, dict):
        return None
    payload = cast(Mapping[str, object], top_provider)
    return _int_or_none(payload.get("max_completion_tokens"))


def _target_with_app_attribution(target: ModelTarget) -> ModelTarget:
    return replace(
        target,
        headers=_merge_headers(_app_attribution_headers(), target.headers),
    )


def _app_attribution_headers() -> dict[str, str]:
    return {
        "HTTP-Referer": _APP_REFERER,
        "X-OpenRouter-Title": _APP_TITLE,
        "X-OpenRouter-Categories": _APP_CATEGORIES,
    }


def _merge_headers(defaults: Mapping[str, str], overrides: Mapping[str, str]) -> dict[str, str]:
    merged = dict(defaults)
    default_keys = {key.lower(): key for key in defaults}
    for key, value in overrides.items():
        existing = default_keys.get(key.lower())
        if existing is not None and existing != key:
            merged.pop(existing, None)
        merged[key] = value
    return merged


def create_model(config: Mapping[str, object]) -> ModelProvider:
    """Create the built-in OpenRouter model provider."""

    del config
    return OpenRouterModelProvider()
