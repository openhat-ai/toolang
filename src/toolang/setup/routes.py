"""Resolve catalog providers and models into effective Toolang facts."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from string import Template
from typing import Protocol, cast

from toolang.base.types.model import (
    Model,
    ModelCatalogSnapshot,
    ModelRoute,
    Provider,
    ProviderToolang,
    ResolvedEnv,
    normalized_env,
    env_names,
)

from toolang.base.types.model import ModelProvider

_CREDENTIAL_SUFFIXES = ("_API_KEY", "_PAT", "_TOKEN")
_APP_ATTRIBUTION_URL = "https://toolang.ai"
_APP_ATTRIBUTION_TITLE = "Toolang"

# Toolang-owned provider conventions: agent-side data keyed by provider id.
PROVIDER_CONVENTIONS: Mapping[str, Mapping[str, object]] = {
    "openrouter": {
        "headers": {
            "HTTP-Referer": _APP_ATTRIBUTION_URL,
            "X-OpenRouter-Title": _APP_ATTRIBUTION_TITLE,
            "X-OpenRouter-Categories": "cli-agent,personal-agent",
        },
    },
    "vercel": {
        "headers": {
            "http-referer": _APP_ATTRIBUTION_URL,
            "x-title": _APP_ATTRIBUTION_TITLE,
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


class RouteAdapter(Protocol):
    """The adapter declaration needed to resolve a route."""

    @property
    def default_api(self) -> str | None: ...


def resolve_catalog_providers(
    snapshot: ModelCatalogSnapshot,
    *,
    adapters: Mapping[str, RouteAdapter],
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
            resolve_model(
                model,
                providers[model._toolang.provider],
                adapters=adapters,
                environ=environ,
            )
            for model in snapshot.models
        ),
        revision=snapshot.revision,
        source=snapshot.source,
        local=snapshot.local,
    )


def resolve_provider(
    provider: Provider,
    *,
    adapters: Mapping[str, RouteAdapter],
    environ: Mapping[str, str],
) -> Provider:
    """Publish a provider default route without changing its declarations."""

    env = _resolve_env(provider)
    satisfied = env if env_is_ready(env, environ=environ) else None
    adapter_name = provider_adapter(provider)
    adapter = adapters.get(adapter_name) if adapter_name is not None else None
    conventions = _convention_block(provider.id)
    default_route = ModelRoute(
        adapter=adapter_name if adapter is not None else None,
        api=_resolve_api(
            _provider_api_template(provider, adapter),
            environ=environ,
            default=None,
        ),
        env=satisfied,
        headers=cast(Mapping[str, str], conventions["headers"]),
        options=cast(Mapping[str, object], conventions["options"]),
    )
    return replace(provider, _toolang=replace(provider._toolang, route=default_route))


def resolve_model(
    model: Model,
    provider: Provider,
    *,
    adapters: Mapping[str, RouteAdapter],
    environ: Mapping[str, str],
) -> Model:
    """Resolve a model against its published provider defaults."""

    name = model_adapter(provider, model)
    implementation = adapters.get(name) if name is not None else None
    mode_blocks = _mode_provider_blocks(model)
    route = ModelRoute(
        adapter=name
        if implementation is not None and mode_blocks is not None
        else None,
        api=_resolve_api(
            _model_api_template(provider, model, implementation),
            environ=environ,
            default=None,
        ),
        env=provider._toolang.route.env,
        headers=model_headers(provider, model, mode_blocks=mode_blocks)
        if mode_blocks is not None
        else {},
        options=model_options(provider, model, mode_blocks=mode_blocks)
        if mode_blocks is not None
        else {},
    )
    return model.with_route(route)


def catalog_environment_names(
    snapshot: ModelCatalogSnapshot, *, adapters: Mapping[str, RouteAdapter]
) -> tuple[str, ...]:
    """Enumerate effective route dependencies, including currently missing names."""

    names: set[str] = set()
    templates: list[str | None] = []
    for provider in snapshot.providers.values():
        names.update(env_names(_resolve_env(provider)))
        adapter = adapters.get(provider_adapter(provider) or "")
        templates.append(_provider_api_template(provider, adapter))
    for model in snapshot.models:
        provider = snapshot.providers[model._toolang.provider]
        adapter = adapters.get(model_adapter(provider, model) or "")
        templates.append(_model_api_template(provider, model, adapter))
    for template in templates:
        for match in Template.pattern.finditer(template or ""):
            name = match.group("named") or match.group("braced")
            if name is not None:
                names.add(name)
    return tuple(sorted(names))


def _api_template(value: str | None, default: str | None) -> str | None:
    return value.strip() if value is not None and value.strip() else default


def _provider_api_template(
    provider: Provider, adapter: RouteAdapter | None
) -> str | None:
    return _api_template(
        provider.api,
        _default_api(
            adapter, npm=provider.npm if not provider._toolang.adapter else None
        ),
    )


def _model_api_template(
    provider: Provider, model: Model, adapter: RouteAdapter | None
) -> str | None:
    override = model.provider or ModelProvider()
    return _api_template(
        _optional_text(override.api) or provider.api,
        _default_api(adapter, npm=_model_npm(provider, model)),
    )


def _default_api(adapter: RouteAdapter | None, *, npm: str | None) -> str | None:
    mapped = _NPM_ROUTES.get(npm or "")
    if mapped is not None and mapped[1] is not None:
        return mapped[1]
    return adapter.default_api if adapter is not None else None


def _model_npm(provider: Provider, model: Model) -> str | None:
    """Use npm defaults only when npm selects the model's adapter."""

    override = model.provider or ModelProvider()
    declared = override._toolang
    if isinstance(declared, ProviderToolang) and declared.adapter:
        return None
    if _normalized_shape(override.shape) is not None:
        return None
    npm = _optional_text(override.npm)
    if npm is not None:
        return npm
    return provider.npm if not provider._toolang.adapter else None


def provider_adapter(provider: Provider) -> str | None:
    """Resolve one catalog provider's declared protocol."""
    if provider._toolang.adapter:
        return provider._toolang.adapter
    route = _NPM_ROUTES.get(provider.npm or "")
    return route[0] if route is not None else None


def model_adapter(provider: Provider, model: Model) -> str | None:
    """Resolve catalog declarations; only setup calls this function."""
    override = model.provider or ModelProvider()
    declared = override._toolang
    if isinstance(declared, ProviderToolang) and declared.adapter:
        return declared.adapter
    shape = _normalized_shape(override.shape)
    if shape is not None:
        return _SHAPE_ADAPTERS.get(shape)
    npm = _optional_text(override.npm)
    if npm is not None:
        mapped = _NPM_ROUTES.get(npm)
        return mapped[0] if mapped is not None else None
    return provider_adapter(provider)


def model_headers(
    provider: Provider, model: Model, *, mode_blocks: tuple[Mapping[str, object], ...]
) -> dict[str, str]:
    """Return the effective request headers for one model."""

    headers: dict[str, str] = {}
    _merge_headers(headers, _convention_block(provider.id).get("headers"))
    override = model.provider or ModelProvider()
    _merge_headers(headers, override.headers)
    for mode_block in mode_blocks:
        _merge_headers(headers, mode_block.get("headers"))
    return headers


def model_options(
    provider: Provider, model: Model, *, mode_blocks: tuple[Mapping[str, object], ...]
) -> dict[str, object]:
    """Return the effective request body options for one model."""

    options: dict[str, object] = {}
    options.update(
        cast(Mapping[str, object], _convention_block(provider.id)["options"])
    )
    override = model.provider or ModelProvider()
    body = override.body
    if isinstance(body, Mapping):
        options.update(body)
    for mode_block in mode_blocks:
        body = mode_block.get("body")
        if isinstance(body, Mapping):
            options.update(cast(Mapping[str, object], body))
    return options


def model_mode(model: Model) -> str | None:
    """Return the catalog mode that applies to one model, when declared."""

    override = model.provider or ModelProvider()
    value = override.mode
    return _optional_text(value)


def _convention_block(provider_id: str) -> Mapping[str, object]:
    block = PROVIDER_CONVENTIONS.get(provider_id, {})
    headers = block.get("headers")
    options = block.get("options")
    return {
        "headers": headers if isinstance(headers, Mapping) else {},
        "options": options if isinstance(options, Mapping) else {},
    }


def _mode_provider_blocks(model: Model) -> tuple[Mapping[str, object], ...] | None:
    """Return selected overrides, or None when the declared mode is invalid."""

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
        return None
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
    template = _api_template(value, default)
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
