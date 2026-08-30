"""Pure model-provider discovery metadata helpers."""

from __future__ import annotations

from collections.abc import Mapping

from toolang.base.types.model import Provider
from toolang.plugin.models.provider_resolver import env_is_ready


def required_provider_env_vars(provider: Provider) -> tuple[str, ...]:
    """Return required environment variables for one provider."""

    resolved = provider.resolved
    if resolved is None:
        return provider.env
    return tuple(
        dict.fromkeys(
            name
            for alternative in resolved.env
            for name in (
                (alternative,) if isinstance(alternative, str) else alternative
            )
        )
    )


def missing_provider_env_vars(
    provider: Provider,
    *,
    environ: Mapping[str, str],
) -> tuple[str, ...]:
    """Return missing required environment variables for one provider."""

    if provider.resolved is not None:
        return (
            ()
            if env_is_ready(provider.resolved.env, environ=environ)
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

    resolved = provider.resolved
    values = resolved.env if resolved is not None else provider.env
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
    if provider.resolved is None:
        raise RuntimeError(f"provider {provider.id!r} has not been resolved")
    return provider.resolved.api


def default_provider_api_key_env(provider: Provider) -> str | None:
    """Return the default API key environment variable for one provider."""

    return provider.env[0] if provider.env else None
