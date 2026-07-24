"""Pure model-provider discovery metadata helpers."""

from __future__ import annotations

from collections.abc import Mapping

from toolang.base.protocols.model import ModelProvider


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
    """Return the default API key environment variable for one provider."""

    try:
        value = provider.default_api_key_env()
    except Exception:
        return None
    if value is None:
        return None
    text = str(value).strip()
    return text or None
