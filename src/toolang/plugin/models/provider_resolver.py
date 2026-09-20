"""Resolve catalog providers and models into effective Toolang facts."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from string import Template
from typing import cast

from toolang.base.protocols.model import ModelAdapter
from toolang.base.types.model import (
    Model,
    ModelCatalogSnapshot,
    ModelRoute,
    Provider,
    ProviderToolang,
    ResolvedEnv,
    env_names,
    normalized_env,
)
from toolang.common.errors import ToolangError

_CREDENTIAL_SUFFIXES = ("_API_KEY", "_PAT", "_TOKEN")

# Toolang-owned provider conventions: agent-side data keyed by provider id.
PROVIDER_CONVENTIONS: Mapping[str, Mapping[str, object]] = {
    "openrouter": {
        "headers": {
            "HTTP-Referer": "https://toolang.ai",
            "X-OpenRouter-Title": "Toolang",
            "X-OpenRouter-Categories": "cli-agent",
        },
    },
}

# npm package -> (adapter, protocol default api)
_NPM_ROUTES: Mapping[str, tuple[str, str | None]] = {
    "@ai-sdk/anthropic": ("messages", "https://api.anthropic.com/v1"),
    "@ai-sdk/cerebras": ("chat_completions", "https://api.cerebras.ai/v1"),
    "@ai-sdk/deepinfra": (
        "chat_completions",
        "https://api.deepinfra.com/v1/openai",
    ),
    "@ai-sdk/gateway": ("chat_completions", "https://ai-gateway.vercel.sh/v1"),
    "@ai-sdk/google": (
        "generate_content",
        "https://generativelanguage.googleapis.com/v1beta",
    ),
    "@ai-sdk/groq": ("chat_completions", "https://api.groq.com/openai/v1"),
    "@ai-sdk/mistral": ("chat_completions", "https://api.mistral.ai/v1"),
    "@ai-sdk/openai": ("responses", "https://api.openai.com/v1"),
    "@ai-sdk/openai-compatible": ("chat_completions", None),
    "@ai-sdk/perplexity": ("chat_completions", "https://api.perplexity.ai"),
    "@ai-sdk/togetherai": ("chat_completions", "https://api.together.xyz/v1"),
    "@ai-sdk/xai": ("chat_completions", "https://api.x.ai/v1"),
    "@openrouter/ai-sdk-provider": ("chat_completions", None),
}
_SHAPE_ADAPTERS: Mapping[str, str] = {
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
) -> ModelCatalogSnapshot:
    """Resolve every provider once and return one frozen snapshot."""

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
        models=tuple(
            providers[model._toolang.provider].models[model.id]
            for model in snapshot.models
        ),
        revision=snapshot.revision,
        source=snapshot.source,
        local=snapshot.local,
    )


def resolve_provider(
    provider: Provider,
    *,
    adapters: Mapping[str, ModelAdapter],
    environ: Mapping[str, str],
) -> Provider:
    """Attach one provider's effective env rule, adapter, and readiness."""

    adapter_name = provider_adapter(provider)
    env = _resolve_env(provider)
    resolved = replace(
        provider,
        _toolang=ProviderToolang(env=env, adapter=adapter_name),
    )
    models = {
        model_id: _resolve_model(
            resolved,
            model,
            adapters=adapters,
            environ=environ,
        )
        for model_id, model in provider.models.items()
    }
    return replace(resolved, models=models)


def provider_adapter(provider: Provider) -> str | None:
    """Return the effective adapter name for one provider."""

    declared = provider._toolang.adapter
    if declared:
        return declared
    if provider.npm is None:
        return None
    route = _NPM_ROUTES.get(provider.npm)
    return route[0] if route is not None else None


def _resolve_model(
    provider: Provider,
    model: Model,
    *,
    adapters: Mapping[str, ModelAdapter],
    environ: Mapping[str, str],
) -> Model:
    override = model.provider or {}
    npm = _optional_text(override.get("npm"))
    shape = _normalized_shape(override.get("shape"))
    provider_adapter_name = provider._toolang.adapter
    if shape is not None:
        model_adapter = _SHAPE_ADAPTERS.get(shape)
    elif npm is not None:
        mapped = _NPM_ROUTES.get(npm)
        model_adapter = mapped[0] if mapped is not None else None
    else:
        model_adapter = None
    if model_adapter is None or model_adapter == provider_adapter_name:
        adapter_name = provider_adapter_name
        resolved_model = model
    else:
        adapter_name = model_adapter
        resolved_model = replace(
            model,
            provider=_with_model_adapter(model.provider, model_adapter),
        )
    adapter = adapters.get(adapter_name) if adapter_name is not None else None
    api = model_api(provider, resolved_model, adapters=adapters, environ=environ)
    ready = (
        adapter is not None
        and api is not None
        and env_is_ready(provider._toolang.env, environ=environ)
    )
    return resolved_model.with_readiness(ready)


def _with_model_adapter(
    value: Mapping[str, object] | None,
    adapter: str,
) -> Mapping[str, object]:
    block = dict(value or {})
    block["_toolang"] = ProviderToolang(adapter=adapter)
    return block


def model_adapter(provider: Provider, model: Model) -> str | None:
    """Return the effective adapter name for one model."""

    override = model.provider or {}
    block = override.get("_toolang")
    # Only our typed value counts: a raw `_toolang` mapping from a source is not
    # runtime configuration.
    if isinstance(block, ProviderToolang) and block.adapter:
        return block.adapter
    return provider._toolang.adapter


def model_route(
    provider: Provider,
    model: Model,
    *,
    adapters: Mapping[str, ModelAdapter],
    environ: Mapping[str, str],
) -> ModelRoute:
    """Compute the effective connection one call must use."""

    adapter_name = model_adapter(provider, model)
    if adapter_name is None:
        raise ToolangError(
            f"model {model.identity} has no adapter for provider {provider.id!r}"
        )
    return ModelRoute(
        provider=provider.id,
        adapter=adapter_name,
        api=model_api(provider, model, adapters=adapters, environ=environ),
        env=provider._toolang.env,
        headers=model_headers(provider, model),
        options=model_options(provider, model),
    )


def model_api(
    provider: Provider,
    model: Model,
    *,
    adapters: Mapping[str, ModelAdapter],
    environ: Mapping[str, str],
) -> str | None:
    """Return the effective API base for one model."""

    override = model.provider or {}
    adapter_name = model_adapter(provider, model)
    adapter = adapters.get(adapter_name) if adapter_name is not None else None
    return _resolve_api(
        _optional_text(override.get("api")) or provider.api,
        environ=environ,
        default=adapter.default_api if adapter is not None else None,
    )


def model_headers(provider: Provider, model: Model) -> dict[str, str]:
    """Return the effective request headers for one model."""

    headers: dict[str, str] = {}
    _merge_headers(headers, _convention_block(provider.id).get("headers"))
    override = model.provider or {}
    _merge_headers(headers, override.get("headers"))
    for mode_block in _mode_provider_blocks(model):
        _merge_headers(headers, mode_block.get("headers"))
    return headers


def model_options(provider: Provider, model: Model) -> dict[str, object]:
    """Return the effective request body options for one model."""

    options: dict[str, object] = {}
    options.update(
        cast(Mapping[str, object], _convention_block(provider.id)["options"])
    )
    override = model.provider or {}
    body = override.get("body")
    if isinstance(body, Mapping):
        options.update(cast(Mapping[str, object], body))
    for mode_block in _mode_provider_blocks(model):
        body = mode_block.get("body")
        if isinstance(body, Mapping):
            options.update(cast(Mapping[str, object], body))
    return options


def model_mode(model: Model) -> str | None:
    """Return the catalog mode that applies to one model, when declared."""

    override = model.provider or {}
    value = override.get("mode")
    return _optional_text(value)


def _convention_block(provider_id: str) -> Mapping[str, object]:
    block = PROVIDER_CONVENTIONS.get(provider_id, {})
    headers = block.get("headers")
    options = block.get("options")
    return {
        "headers": headers if isinstance(headers, Mapping) else {},
        "options": options if isinstance(options, Mapping) else {},
    }


def _mode_provider_blocks(model: Model) -> tuple[Mapping[str, object], ...]:
    mode = model_mode(model)
    if mode is None:
        return ()
    experimental = model.experimental
    raw_modes = experimental.get("modes") if isinstance(experimental, Mapping) else None
    modes = (
        cast(Mapping[str, object], raw_modes)
        if isinstance(raw_modes, Mapping)
        else None
    )
    selected = modes.get(mode) if modes is not None else None
    if not isinstance(selected, Mapping):
        raise ToolangError(f"model {model.identity} does not advertise mode {mode!r}")
    block = cast(Mapping[str, object], selected).get("provider")
    return (cast(Mapping[str, object], block),) if isinstance(block, Mapping) else ()


def _merge_headers(target: dict[str, str], value: object) -> None:
    if not isinstance(value, Mapping):
        return
    lowered = {key.lower(): key for key in target}
    for key, item in cast(Mapping[str, object], value).items():
        if not isinstance(key, str) or not isinstance(item, str):
            continue
        existing = lowered.get(key.lower())
        if existing is not None and existing != key:
            target.pop(existing, None)
        target[key] = item
        lowered[key.lower()] = key


def trimmed_environ(
    provider: Provider,
    *,
    environ: Mapping[str, str],
) -> dict[str, str]:
    """Return the environment trimmed to the names one provider declares."""

    return {
        name: environ[name]
        for name in env_names(provider._toolang.env)
        if name in environ
    }


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

    for alternative in provider._toolang.env:
        names = (alternative,) if isinstance(alternative, str) else alternative
        if all(_env_value(environ, name) for name in names):
            return names
    return ()


def credential_value(
    env: ResolvedEnv,
    *,
    environ: Mapping[str, str],
) -> str | None:
    """Select one opaque credential value from a satisfied env rule."""

    names: tuple[str, ...] = ()
    for alternative in env:
        candidate = (alternative,) if isinstance(alternative, str) else alternative
        if all(_env_value(environ, name) for name in candidate):
            names = candidate
            break
    credential = next(
        (name for name in names if name.endswith(_CREDENTIAL_SUFFIXES)),
        names[-1] if names else None,
    )
    return environ.get(credential) if credential is not None else None


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


def _resolve_env(provider: Provider) -> ResolvedEnv:
    override = _ENV_OVERRIDES.get(provider.id)
    if override is not None:
        return override
    if provider._toolang.env:
        return normalized_env(provider._toolang.env)
    credentials = tuple(
        name for name in provider.env if name.endswith(_CREDENTIAL_SUFFIXES)
    )
    required = tuple(
        name for name in provider.env if not name.endswith(_CREDENTIAL_SUFFIXES)
    )
    if credentials:
        return normalized_env(tuple((*required, name) for name in credentials))
    return normalized_env((required,)) if required else ()


def _resolve_api(
    value: str | None,
    *,
    environ: Mapping[str, str],
    default: str | None,
) -> str | None:
    template = value.strip() if value is not None and value.strip() else default
    if template is None:
        return None
    try:
        api = Template(template).substitute(environ).strip()
    except (KeyError, ValueError):
        return None
    return api or None


def _optional_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _normalized_shape(value: object) -> str | None:
    text = _optional_text(value)
    return text.lower().replace("-", "_").replace(" ", "_") if text else None


def _env_value(environ: Mapping[str, str], name: str) -> bool:
    return bool(str(environ.get(name, "")).strip())
