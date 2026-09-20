"""Pure model-provider discovery metadata helpers."""

from __future__ import annotations

from collections.abc import Mapping

from toolang.base.types.model import Provider, env_names
from toolang.plugin.models.provider_resolver import env_is_ready


def required_provider_env_vars(provider: Provider) -> tuple[str, ...]:
    """Return required environment variables for one provider."""

    return env_names(provider._toolang.env)


def missing_provider_env_vars(
    provider: Provider,
    *,
    environ: Mapping[str, str],
) -> tuple[str, ...]:
    """Return missing required environment variables for one provider."""

    if provider._toolang.env:
        return (
            ()
            if env_is_ready(provider._toolang.env, environ=environ)
            else required_provider_env_vars(provider)
        )
    return tuple(
        name
        for name in required_provider_env_vars(provider)
        if not str(environ.get(name, "")).strip()
    )


def absent_provider_env_vars(
    provider: Provider,
    *,
    environ: Mapping[str, str],
) -> tuple[str, ...]:
    """Return individually absent environment variables for presentation/querying."""

    return tuple(
        name
        for name in required_provider_env_vars(provider)
        if not str(environ.get(name, "")).strip()
    )


def provider_env_requirements(provider: Provider) -> tuple[str, ...]:
    """Return displayable OR alternatives with AND groups joined by `` + ``."""

    values = provider._toolang.env
    return tuple(
        alternative if isinstance(alternative, str) else " + ".join(alternative)
        for alternative in values
    )


def default_provider_base_url(
    provider: Provider,
    *,
    environ: Mapping[str, str],
) -> str | None:
    """Return the default API base URL for one provider when known."""

    del environ
    return provider.api


def default_provider_api_key_env(provider: Provider) -> str | None:
    """Return the default API key environment variable for one provider."""

    names = env_names(provider._toolang.env)
    return names[0] if names else None
