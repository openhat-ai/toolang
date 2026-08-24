"""Resolve raw catalog providers once into immutable runtime facts."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from string import Template
from typing import cast

from toolang.base.protocols.model import ModelAdapter
from toolang.base.types.model import (
    Model,
    ModelCatalogSnapshot,
    Provider,
    ResolvedEnv,
    ResolvedModel,
    ResolvedProvider,
)
from toolang.plugin.models.config import ProviderConfig

_CREDENTIAL_SUFFIXES = ("_API_KEY", "_PAT", "_TOKEN")


@dataclass(frozen=True, slots=True)
class _ProtocolRoute:
    adapter: str
    endpoint: str | None = None


_NPM_ROUTES = {
    "@ai-sdk/anthropic": _ProtocolRoute("messages"),
    "@ai-sdk/cerebras": _ProtocolRoute(
        "chat_completions", "https://api.cerebras.ai/v1"
    ),
    "@ai-sdk/deepinfra": _ProtocolRoute(
        "chat_completions", "https://api.deepinfra.com/v1/openai"
    ),
    "@ai-sdk/gateway": _ProtocolRoute(
        "chat_completions", "https://ai-gateway.vercel.sh/v1"
    ),
    "@ai-sdk/google": _ProtocolRoute("generate_content"),
    "@ai-sdk/groq": _ProtocolRoute(
        "chat_completions", "https://api.groq.com/openai/v1"
    ),
    "@ai-sdk/mistral": _ProtocolRoute("chat_completions", "https://api.mistral.ai/v1"),
    "@ai-sdk/openai": _ProtocolRoute("responses"),
    "@ai-sdk/openai-compatible": _ProtocolRoute("chat_completions"),
    "@ai-sdk/perplexity": _ProtocolRoute(
        "chat_completions", "https://api.perplexity.ai"
    ),
    "@ai-sdk/togetherai": _ProtocolRoute(
        "chat_completions", "https://api.together.xyz/v1"
    ),
    "@ai-sdk/xai": _ProtocolRoute("chat_completions", "https://api.x.ai/v1"),
    "@openrouter/ai-sdk-provider": _ProtocolRoute("chat_completions"),
}
_SHAPE_ADAPTERS = {
    "chat_completions": "chat_completions",
    "completions": "chat_completions",
    "generate_content": "generate_content",
    "messages": "messages",
    "responses": "responses",
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
    configs: Mapping[str, ProviderConfig] | None = None,
) -> ModelCatalogSnapshot:
    """Resolve every provider exactly once and return one frozen snapshot."""

    providers = {
        provider_id: resolve_provider(
            provider,
            adapters=adapters,
            environ=environ,
            config=(configs or {}).get(provider_id),
        )
        for provider_id, provider in snapshot.providers.items()
    }
    return ModelCatalogSnapshot(
        providers=providers,
        models=tuple(
            providers[model.provider_id].models[model.id] for model in snapshot.models
        ),
        revision=snapshot.revision,
        source=snapshot.source,
    )


def resolve_provider(
    provider: Provider,
    *,
    adapters: Mapping[str, ModelAdapter],
    environ: Mapping[str, str],
    config: ProviderConfig | None = None,
) -> Provider:
    """Attach one provider's adapter, endpoint, env rule, and readiness."""

    provider_route = _NPM_ROUTES.get(provider.npm)
    adapter_name = _configured_adapter(config) or (
        provider_route.adapter if provider_route is not None else None
    )
    adapter = adapters.get(adapter_name) if adapter_name is not None else None
    endpoint = _resolve_endpoint(
        config.endpoint if config is not None and config.endpoint else provider.api,
        environ=environ,
        default=(
            provider_route.endpoint
            if (config is None or config.adapter is None)
            and provider_route is not None
            and provider_route.endpoint is not None
            else adapter.default_endpoint
            if adapter is not None
            else None
        ),
    )
    env = _resolve_env(
        provider,
        names=(config.key_env,)
        if config is not None and config.key_env is not None
        else provider.env,
        provider_override=config is None or config.key_env is None,
    )
    ready = (
        adapter is not None
        and endpoint is not None
        and env_is_ready(env, environ=environ)
        and not _local_provider_offline(provider)
    )
    default = ResolvedProvider(
        adapter=adapter_name,
        endpoint=endpoint,
        env=env,
        ready=ready,
    )
    models = {
        model_id: _resolve_model(
            provider,
            model,
            default=default,
            adapters=adapters,
            environ=environ,
            config=config,
        )
        for model_id, model in provider.models.items()
    }
    return replace(
        provider,
        models=models,
        resolved=default,
    )


def _resolve_model(
    provider: Provider,
    model: Model,
    *,
    default: ResolvedProvider,
    adapters: Mapping[str, ModelAdapter],
    environ: Mapping[str, str],
    config: ProviderConfig | None,
) -> Model:
    override = model.provider or {}
    npm = _optional_text(override.get("npm"))
    shape = _normalized_shape(override.get("shape"))
    route = _NPM_ROUTES.get(npm) if npm is not None else None
    if _configured_adapter(config) is not None:
        adapter_name = _configured_adapter(config)
    elif shape is not None:
        adapter_name = _SHAPE_ADAPTERS.get(shape)
    elif npm is not None:
        adapter_name = route.adapter if route is not None else None
    else:
        adapter_name = default.adapter
    adapter = adapters.get(adapter_name) if adapter_name is not None else None
    configured_endpoint = config.endpoint if config is not None else None
    endpoint_value = (
        configured_endpoint or _optional_text(override.get("api")) or provider.api
    )
    route_endpoint = (
        route.endpoint
        if (config is None or config.adapter is None) and route is not None
        else None
    )
    endpoint = _resolve_endpoint(
        endpoint_value,
        environ=environ,
        default=(
            route_endpoint
            or (adapter.default_endpoint if adapter is not None else None)
        ),
    )
    ready = (
        adapter is not None
        and endpoint is not None
        and env_is_ready(default.env, environ=environ)
        and not _local_provider_offline(provider)
    )
    return replace(
        model,
        resolved=ResolvedModel(
            adapter=adapter_name,
            endpoint=endpoint,
            ready=ready,
        ),
    )


def _configured_adapter(config: ProviderConfig | None) -> str | None:
    return config.adapter if config is not None else None


def _normalized_shape(value: object) -> str | None:
    text = _optional_text(value)
    return text.lower().replace("-", "_").replace(" ", "_") if text else None


def _optional_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


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


def _resolve_env(
    provider: Provider,
    *,
    names: tuple[str, ...],
    provider_override: bool = True,
) -> ResolvedEnv:
    override = _ENV_OVERRIDES.get(provider.id) if provider_override else None
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
