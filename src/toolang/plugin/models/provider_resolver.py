"""Resolve raw catalog providers once into immutable runtime facts."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from string import Template
from typing import cast

from toolang.base.protocols.model import ModelAdapter
from toolang.base.types.model import (
    ModelCatalogSnapshot,
    Provider,
    ResolvedEnv,
    ResolvedProvider,
)
from toolang.plugin.models.config import catalog_provider_config

_CREDENTIAL_SUFFIXES = ("_API_KEY", "_PAT", "_TOKEN")
_NPM_ADAPTERS = {
    "@ai-sdk/anthropic": "messages",
    "@ai-sdk/google": "generate_content",
    "@ai-sdk/openai": "responses",
    "@ai-sdk/openai-compatible": "chat_completions",
    "@openrouter/ai-sdk-provider": "chat_completions",
}
_ENV_OVERRIDES: Mapping[str, ResolvedEnv] = {
    "amazon-bedrock": (
        ("AWS_BEARER_TOKEN_BEDROCK", "AWS_REGION"),
        ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_REGION"),
    ),
}


def resolve_catalog_providers(
    snapshot: ModelCatalogSnapshot,
    *,
    adapters: Mapping[str, ModelAdapter],
    environ: Mapping[str, str],
) -> ModelCatalogSnapshot:
    """Resolve every provider exactly once and return one frozen snapshot."""

    providers = {
        provider_id: resolve_provider(
            provider,
            adapters=adapters,
            environ=environ,
        )
        for provider_id, provider in snapshot.providers.items()
    }
    return ModelCatalogSnapshot(
        providers=providers,
        models=snapshot.models,
        revision=snapshot.revision,
        source=snapshot.source,
    )


def resolve_provider(
    provider: Provider,
    *,
    adapters: Mapping[str, ModelAdapter],
    environ: Mapping[str, str],
) -> Provider:
    """Attach one provider's adapter, endpoint, env rule, and readiness."""

    config = catalog_provider_config(provider)
    adapter_name = (
        config.adapter
        if config is not None and config.adapter is not None
        else _NPM_ADAPTERS.get(provider.npm)
    )
    adapter = adapters.get(adapter_name) if adapter_name is not None else None
    endpoint = _resolve_endpoint(
        config.endpoint if config is not None and config.endpoint else provider.api,
        environ=environ,
        default=adapter.default_endpoint if adapter is not None else None,
    )
    env = _resolve_env(
        provider,
        names=(config.key_env,)
        if config is not None and config.key_env is not None
        else provider.env,
    )
    ready = (
        adapter is not None
        and endpoint is not None
        and env_is_ready(env, environ=environ)
        and not _local_provider_offline(provider)
    )
    return replace(
        provider,
        resolved=ResolvedProvider(
            adapter=adapter_name,
            endpoint=endpoint,
            env=env,
            ready=ready,
        ),
    )


def env_is_ready(env: ResolvedEnv, *, environ: Mapping[str, str]) -> bool:
    """Return whether one OR-of-AND environment rule is satisfied."""

    if not env:
        return True
    return any(
        _env_value(environ, alternative)
        if isinstance(alternative, str)
        else all(_env_value(environ, name) for name in alternative)
        for alternative in env
    )


def selected_env_names(
    provider: Provider,
    *,
    environ: Mapping[str, str],
) -> tuple[str, ...]:
    """Return the first satisfied resolved env alternative."""

    resolved = provider.resolved
    if resolved is None:
        raise RuntimeError(f"provider {provider.id!r} has not been resolved")
    for alternative in resolved.env:
        names = (alternative,) if isinstance(alternative, str) else alternative
        if all(_env_value(environ, name) for name in names):
            return names
    return ()


def selected_credential_value(
    provider: Provider,
    *,
    environ: Mapping[str, str],
) -> str | None:
    """Select one opaque credential value from a satisfied env alternative."""

    names = selected_env_names(provider, environ=environ)
    credential = next(
        (name for name in names if name.endswith(_CREDENTIAL_SUFFIXES)),
        names[-1] if names else None,
    )
    return environ.get(credential) if credential is not None else None


def _resolve_env(provider: Provider, *, names: tuple[str, ...]) -> ResolvedEnv:
    override = _ENV_OVERRIDES.get(provider.id)
    if override is not None:
        return override
    credentials = tuple(name for name in names if name.endswith(_CREDENTIAL_SUFFIXES))
    required = tuple(name for name in names if name not in credentials)
    if credentials:
        return tuple(
            _compact_group((*required, credential)) for credential in credentials
        )
    if required:
        return (_compact_group(required),)
    return ()


def _compact_group(names: tuple[str, ...]) -> str | tuple[str, ...]:
    return names[0] if len(names) == 1 else names


def _resolve_endpoint(
    value: str | None,
    *,
    environ: Mapping[str, str],
    default: str | None,
) -> str | None:
    template = value.strip() if value is not None and value.strip() else default
    if template is None:
        return None
    try:
        endpoint = Template(template).substitute(environ).strip()
    except (KeyError, ValueError):
        return None
    return endpoint or None


def _env_value(environ: Mapping[str, str], name: str) -> bool:
    return bool(str(environ.get(name, "")).strip())


def _local_provider_offline(provider: Provider) -> bool:
    if not provider.local:
        return False
    runtime = provider.extra.get("runtime")
    return (
        isinstance(runtime, Mapping)
        and cast(Mapping[str, object], runtime).get("status") == "offline"
    )
