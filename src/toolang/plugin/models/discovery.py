"""Pure model-provider discovery metadata helpers."""

from __future__ import annotations

from collections.abc import Mapping

from toolang.base.protocols.model import ModelProvider
from toolang.base.types.model import Provider

_DEFAULT_PROVIDER_URLS = {
    "google": "https://generativelanguage.googleapis.com/v1beta/openai/",
    "openai": "https://api.openai.com/v1",
}


def required_provider_env_vars(provider: ModelProvider | Provider) -> tuple[str, ...]:
    """Return required environment variables for one provider."""

    if isinstance(provider, Provider):
        return provider.env
    try:
        return tuple(var for var in provider.required_env_vars() if str(var).strip())
    except Exception:
        return ()


def missing_provider_env_vars(
    provider: ModelProvider | Provider,
    *,
    environ: Mapping[str, str],
) -> tuple[str, ...]:
    """Return missing required environment variables for one provider."""

    if isinstance(provider, Provider) and provider.env:
        return (
            ()
            if any(str(environ.get(name, "")).strip() for name in provider.env)
            else provider.env
        )
    return tuple(
        name
        for name in required_provider_env_vars(provider)
        if not str(environ.get(name, "")).strip()
    )


def default_provider_base_url(
    provider: ModelProvider | Provider,
    *,
    environ: Mapping[str, str],
) -> str | None:
    """Return the default API base URL for one provider when known."""

    if isinstance(provider, Provider):
        return provider.api or _DEFAULT_PROVIDER_URLS.get(provider.id)
    try:
        value = provider.default_base_url(environ=environ)
    except Exception:
        return None
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def default_provider_api_key_env(provider: ModelProvider | Provider) -> str | None:
    """Return the default API key environment variable for one provider."""

    if isinstance(provider, Provider):
        return provider.env[0] if provider.env else None
    try:
        value = provider.default_api_key_env()
    except Exception:
        return None
    if value is None:
        return None
    text = str(value).strip()
    return text or None
