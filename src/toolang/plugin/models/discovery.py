"""Pure model-provider discovery metadata helpers."""

from __future__ import annotations

from collections.abc import Mapping

from toolang.base.types.model import Provider, env_names


def required_provider_env_vars(provider: Provider) -> tuple[str, ...]:
    """Return required environment variables for one provider."""

    return env_names(provider._toolang.env)


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
