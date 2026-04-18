"""Shared model provider discovery helpers."""

from __future__ import annotations

from collections.abc import Mapping
from time import monotonic
from weakref import WeakKeyDictionary

from toolang.base.protocols.model import ModelProvider
from toolang.base.types.model import ModelInfo

_MODEL_INFO_CACHE_TTL_SEC = 30.0
_MODEL_INFO_CACHE: WeakKeyDictionary[
    ModelProvider,
    dict[tuple[str | None, tuple[tuple[str, str], ...]], tuple[float, tuple[ModelInfo, ...]]],
] = WeakKeyDictionary()


def required_provider_env_vars(provider: ModelProvider) -> tuple[str, ...]:
    """Return required environment variables for one provider."""

    try:
        return tuple(var for var in provider.required_env_vars() if str(var).strip())
    except Exception:
        return ()


def missing_provider_env_vars(
    provider: ModelProvider,
    *,
    environ: Mapping[str, str],
) -> tuple[str, ...]:
    """Return missing required environment variables for one provider."""

    return tuple(
        name
        for name in required_provider_env_vars(provider)
        if not str(environ.get(name, "")).strip()
    )


def default_provider_base_url(
    provider: ModelProvider,
    *,
    environ: Mapping[str, str],
) -> str | None:
    """Return the default API base URL for one provider when known."""

    try:
        value = provider.default_base_url(environ=environ)
    except Exception:
        return None
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def default_provider_api_key_env(provider: ModelProvider) -> str | None:
    """Return the default API key environment variable for one provider when known."""

    try:
        value = provider.default_api_key_env()
    except Exception:
        return None
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def model_infos(
    provider: ModelProvider,
    *,
    environ: Mapping[str, str],
) -> tuple[ModelInfo, ...]:
    """Return model infos exposed by one provider."""

    cache_bucket = _MODEL_INFO_CACHE.setdefault(provider, {})
    cache_key = _model_info_cache_key(provider, environ=environ)
    now = monotonic()
    cached = cache_bucket.get(cache_key)
    if cached is not None:
        cached_at, infos = cached
        if now - cached_at < _MODEL_INFO_CACHE_TTL_SEC:
            return infos
    try:
        infos = tuple(provider.list_models(environ=environ))
    except Exception:
        return ()
    cache_bucket[cache_key] = (now, infos)
    return infos


def _model_info_cache_key(
    provider: ModelProvider,
    *,
    environ: Mapping[str, str],
) -> tuple[str | None, tuple[tuple[str, str], ...]]:
    base_url = default_provider_base_url(provider, environ=environ)
    required = tuple(
        sorted(
            (name, str(environ.get(name, "")))
            for name in required_provider_env_vars(provider)
        )
    )
    del provider
    return (base_url, required)
