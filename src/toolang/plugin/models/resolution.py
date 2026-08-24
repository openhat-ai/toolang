"""Model selector and target resolution."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from fnmatch import fnmatchcase
from string import Template
from typing import Protocol, TypeAlias, cast

from toolang.base.errors import ToolangError
from toolang.base.protocols.model import ModelProvider
from toolang.base.types.model import Model, ModelAlias, ModelInfo, ModelTarget, Provider
from toolang.plugin.models.config import catalog_provider_config
from toolang.plugin.models.discovery import (
    default_provider_api_key_env,
    default_provider_base_url,
    required_provider_env_vars,
)
from toolang.plugin.models.messages import (
    NO_AVAILABLE_MODELS_MESSAGE,
    NO_MATCHED_MODELS_MESSAGE,
)
from toolang.common.selectors import (
    Selector as ModelSelector,
    filter_value_matches,
    parse_selector,
    selector_identity_matches,
    split_selector_list,
)

DEFAULT_MODEL_SELECTOR = "gpt-5"
CUSTOM_MODEL_PROVIDER = "custom"
CatalogProvider: TypeAlias = ModelProvider | Provider


def split_model_selectors(items: list[str] | tuple[str, ...] | None) -> tuple[str, ...]:
    """Split repeated and CSV model selector inputs."""

    return split_selector_list(items)


def parse_model_selector(raw: str) -> ModelSelector:
    """Parse one model selector."""

    return parse_selector(raw, domain="model")


def resolve_catalog_adapter(
    provider: CatalogProvider,
    *,
    model: Model | None = None,
) -> str | None:
    """Resolve a protocol adapter from explicit config and catalog signals."""

    if isinstance(provider, Provider):
        config = catalog_provider_config(provider)
        if config is not None and config.adapter is not None:
            return config.adapter
        model_provider = model.provider if model is not None else None
        model_npm = (
            model_provider.get("npm") if isinstance(model_provider, Mapping) else None
        )
        npm = model_npm if isinstance(model_npm, str) else provider.npm
        shape = (
            model_provider.get("shape") if isinstance(model_provider, Mapping) else None
        )
        if shape in {"completions", "chat_completions"}:
            return "chat_completions"
        if provider.id == "openai" or npm == "@ai-sdk/openai":
            return "responses"
        if provider.id == "anthropic" or npm == "@ai-sdk/anthropic":
            return "messages"
        if provider.id in {
            "custom",
            "deepseek",
            "google",
            "llama_cpp",
            "ollama",
            "openrouter",
        } or npm in {
            "@ai-sdk/openai-compatible",
            "@openrouter/ai-sdk-provider",
        }:
            return "chat_completions"
        return None
    return _default_provider_adapter(provider.name)


def catalog_model_endpoint(
    provider: CatalogProvider,
    model: Model,
    *,
    envs: Mapping[str, str],
) -> str | None:
    """Resolve the configured or catalog endpoint for one model record."""

    model_provider = model.provider
    value = model_provider.get("api") if isinstance(model_provider, Mapping) else None
    if isinstance(value, str) and value.strip():
        return Template(value.strip()).safe_substitute(envs)
    return default_provider_base_url(provider, environ=envs)


class SupportsModelSelection(Protocol):
    """Minimal context shape needed to resolve model selectors."""

    providers: Mapping[str, CatalogProvider]
    models: tuple[ModelInfo, ...]
    model_aliases: Mapping[str, ModelAlias]
    default_models: tuple[str, ...]
    envs: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class _Candidate:
    selector: str
    target: ModelTarget
    match_values: tuple[str, ...]
    alias: ModelAlias | None = None
    info: ModelInfo | None = None


@dataclass(frozen=True, slots=True)
class ModelTargetResolver:
    """Resolve model catalog entries and runtime configuration into targets."""

    providers: Mapping[str, CatalogProvider]
    models: tuple[ModelInfo, ...]
    model_aliases: Mapping[str, ModelAlias]
    default_models: tuple[str, ...]
    envs: Mapping[str, str]

    def resolve(
        self,
        selector: str | None,
        *,
        default_selector: str | None = None,
        allowed_selectors: Sequence[str] | None = None,
    ) -> ModelTarget:
        return resolve_model(
            self,
            selector=selector,
            default_selector=default_selector,
            allowed_selectors=allowed_selectors,
        )

    def selectable(
        self,
        selectors: Sequence[str] | None = None,
    ) -> tuple[tuple[str, ModelTarget], ...]:
        return selectable_model_targets(
            providers=self.providers,
            models=self.models,
            aliases=self.model_aliases,
            envs=self.envs,
            selectors=selectors,
        )


def resolve_model(
    context: SupportsModelSelection,
    *,
    selector: str | None,
    default_selector: str | None = None,
    allowed_selectors: Sequence[str] | None = None,
) -> ModelTarget:
    """Resolve one model selector against one uptime context."""

    resolved_allowed = _resolve_allowed_targets(
        allowed_selectors,
        providers=context.providers,
        models=context.models,
        aliases=context.model_aliases,
        envs=context.envs,
    )
    effective_selector = _first_non_empty(
        selector,
        default_selector,
        *context.default_models,
        DEFAULT_MODEL_SELECTOR,
    )
    matches = _resolve_selector_targets(
        (effective_selector,),
        providers=context.providers,
        models=context.models,
        aliases=context.model_aliases,
        envs=context.envs,
    )
    if not matches:
        raise ToolangError(
            _empty_model_selection_message(
                providers=context.providers,
                models=context.models,
                aliases=context.model_aliases,
                envs=context.envs,
            )
        )
    if len(matches) > 1:
        joined = ", ".join(item.selector for item in matches)
        raise ToolangError(
            f"model selector is ambiguous: {effective_selector} (matches {joined})"
        )
    target = matches[0].target
    _require_allowed(target, selector=effective_selector, allowed=resolved_allowed)
    return target


def select_model_selectors(
    context: SupportsModelSelection,
    *,
    directive_selectors: Sequence[str] = (),
    allowed_selectors: Sequence[str] = (),
    default_selector: str | None = None,
) -> tuple[str, ...]:
    """Return the effective ordered model selectors for one run."""

    directive_candidates = _resolve_selector_targets(
        directive_selectors,
        providers=context.providers,
        models=context.models,
        aliases=context.model_aliases,
        envs=context.envs,
    )
    allowed_candidates = _resolve_selector_targets(
        allowed_selectors,
        providers=context.providers,
        models=context.models,
        aliases=context.model_aliases,
        envs=context.envs,
    )
    if directive_selectors and not directive_candidates:
        raise ToolangError(
            _empty_model_selection_message(
                providers=context.providers,
                models=context.models,
                aliases=context.model_aliases,
                envs=context.envs,
            )
        )
    if allowed_selectors and not allowed_candidates:
        raise ToolangError(
            _empty_model_selection_message(
                providers=context.providers,
                models=context.models,
                aliases=context.model_aliases,
                envs=context.envs,
            )
        )
    if directive_candidates and allowed_candidates:
        directive_identities = {
            _target_identity(candidate.target) for candidate in directive_candidates
        }
        selected = tuple(
            candidate.selector
            for candidate in allowed_candidates
            if _target_identity(candidate.target) in directive_identities
        )
        if selected:
            return selected
        raise ToolangError(NO_MATCHED_MODELS_MESSAGE)

    if allowed_candidates:
        return _dedupe(candidate.selector for candidate in allowed_candidates)

    if directive_candidates:
        return _dedupe(candidate.selector for candidate in directive_candidates)

    available = _discover_available_candidates(
        providers=context.providers,
        models=context.models,
        aliases=context.model_aliases,
        envs=context.envs,
    )
    defaults = (
        (default_selector,)
        if default_selector and default_selector.strip()
        else tuple(context.default_models)
    ) or (DEFAULT_MODEL_SELECTOR,)
    preferred = _resolve_selector_targets(
        defaults,
        providers=context.providers,
        models=context.models,
        aliases=context.model_aliases,
        envs=context.envs,
    )
    ordered: list[str] = []
    seen: set[str] = set()
    for candidate in (*preferred, *available):
        if candidate.selector in seen:
            continue
        seen.add(candidate.selector)
        ordered.append(candidate.selector)
    if ordered:
        return tuple(ordered)
    raise ToolangError(
        _empty_model_selection_message(
            providers=context.providers,
            models=context.models,
            aliases=context.model_aliases,
            envs=context.envs,
        )
    )


def selectable_model_targets(
    *,
    providers: Mapping[str, CatalogProvider],
    models: Sequence[ModelInfo],
    aliases: Mapping[str, ModelAlias],
    envs: Mapping[str, str],
    selectors: Sequence[str] | None = None,
) -> tuple[tuple[str, ModelTarget], ...]:
    """Return selectable model targets for CLI/API listing."""

    if selectors:
        return tuple(
            (candidate.selector, candidate.target)
            for candidate in _resolve_selector_targets(
                selectors,
                providers=providers,
                models=models,
                aliases=aliases,
                envs=envs,
            )
        )
    return tuple(
        (candidate.selector, candidate.target)
        for candidate in _discover_available_candidates(
            providers=providers,
            models=models,
            aliases=aliases,
            envs=envs,
        )
    )


def _resolve_allowed_targets(
    selectors: Sequence[str] | None,
    *,
    providers: Mapping[str, CatalogProvider],
    models: Sequence[ModelInfo],
    aliases: Mapping[str, ModelAlias],
    envs: Mapping[str, str],
) -> tuple[ModelTarget, ...]:
    targets: list[ModelTarget] = []
    for candidate in _resolve_selector_targets(
        selectors,
        providers=providers,
        models=models,
        aliases=aliases,
        envs=envs,
    ):
        targets.append(candidate.target)
    return tuple(targets)


def _discover_available_candidates(
    *,
    providers: Mapping[str, CatalogProvider],
    models: Sequence[ModelInfo],
    aliases: Mapping[str, ModelAlias],
    envs: Mapping[str, str],
) -> tuple[_Candidate, ...]:
    candidates: list[_Candidate] = []
    seen: set[tuple[str, str, str, str | None]] = set()
    for name, alias in aliases.items():
        target = _target_from_alias(
            alias,
            providers=providers,
            models=models,
            envs=envs,
            strict=False,
        )
        if target is None:
            continue
        identity = _target_identity(target)
        if identity in seen:
            continue
        seen.add(identity)
        candidates.append(
            _Candidate(
                selector=name,
                target=target,
                match_values=_candidate_match_values(name, target),
                alias=alias,
            )
        )
    for info in models:
        provider = providers.get(info.provider)
        if provider is None or _provider_id(provider) == CUSTOM_MODEL_PROVIDER:
            continue
        if _missing_target_env_vars(provider, alias=None, envs=envs):
            continue
        if info.adapter == "unavailable":
            continue
        target = _target_from_info(provider, info, envs=envs)
        identity = _target_identity(target)
        if identity in seen:
            continue
        seen.add(identity)
        selector = f"{info.ref}[{_provider_id(provider)}]"
        candidates.append(
            _Candidate(
                selector=selector,
                target=target,
                match_values=_candidate_match_values(selector, target, *info.selectors),
                info=info,
            )
        )
    return tuple(candidates)


def _resolve_selector_targets(
    selectors: Sequence[str] | None,
    *,
    providers: Mapping[str, CatalogProvider],
    models: Sequence[ModelInfo],
    aliases: Mapping[str, ModelAlias],
    envs: Mapping[str, str],
) -> tuple[_Candidate, ...]:
    candidates = _discover_available_candidates(
        providers=providers,
        models=models,
        aliases=aliases,
        envs=envs,
    )
    selected: list[_Candidate] = []
    seen: set[tuple[str, str, str, str | None]] = set()
    for raw in selectors or ():
        text = raw.strip()
        if not text:
            continue
        if text in aliases:
            target = _target_from_alias(
                aliases[text],
                providers=providers,
                models=models,
                envs=envs,
                strict=True,
            )
            if target is None:
                continue
            identity = _target_identity(target)
            if identity not in seen:
                seen.add(identity)
                selected.append(
                    _Candidate(
                        selector=text,
                        target=target,
                        match_values=_candidate_match_values(text, target),
                        alias=aliases[text],
                    )
                )
            continue
        selector = parse_model_selector(text)
        matches = tuple(
            candidate
            for candidate in candidates
            if _candidate_matches(candidate, selector)
        )
        if not matches and _looks_exact_ref(selector) and not selector.filters:
            matches = _resolve_exact_ref(
                selector.pattern,
                providers=providers,
                models=models,
                aliases=aliases,
                envs=envs,
            )
        for candidate in matches:
            identity = _target_identity(candidate.target)
            if identity in seen:
                continue
            seen.add(identity)
            selected.append(candidate)
    return tuple(selected)


def _resolve_exact_ref(
    ref: str,
    *,
    providers: Mapping[str, CatalogProvider],
    models: Sequence[ModelInfo],
    aliases: Mapping[str, ModelAlias],
    envs: Mapping[str, str],
) -> tuple[_Candidate, ...]:
    matches: list[_Candidate] = []
    for info in models:
        if info.ref != ref:
            continue
        provider = providers.get(info.provider)
        if provider is None or _provider_id(provider) == CUSTOM_MODEL_PROVIDER:
            continue
        if _missing_target_env_vars(provider, alias=None, envs=envs):
            continue
        if info.adapter == "unavailable":
            continue
        target = _target_from_info(provider, info, envs=envs)
        matches.append(
            _Candidate(
                selector=f"{target.ref}[{_provider_id(provider)}]",
                target=target,
                match_values=_candidate_match_values(ref, target),
                info=info,
            )
        )
    for name, alias in aliases.items():
        if alias.ref != ref:
            continue
        target = _target_from_alias(
            alias,
            providers=providers,
            models=models,
            envs=envs,
            strict=False,
        )
        if target is None:
            continue
        matches.append(
            _Candidate(
                selector=name,
                target=target,
                match_values=_candidate_match_values(name, target),
                alias=alias,
            )
        )
    return tuple(matches)


def _candidate_matches(candidate: _Candidate, selector: ModelSelector) -> bool:
    if _looks_exact_ref(selector) and candidate.target.ref != selector.pattern:
        return False
    if not _pattern_matches(candidate, selector.pattern):
        return False
    for key, values in selector.filters.items():
        actual_values = _candidate_filter_values(candidate, key)
        if not actual_values or not any(
            filter_value_matches(actual, values) for actual in actual_values
        ):
            return False
    return True


def _pattern_matches(candidate: _Candidate, pattern: str) -> bool:
    text = pattern.strip() or "*"
    if text == "*":
        return True
    if "/" in text:
        family, _, name = candidate.target.ref.partition("/")
        if selector_identity_matches(
            family=family,
            name=name or candidate.target.model,
            selector=ModelSelector(raw=text, pattern=text),
            extra_values=candidate.match_values,
        ):
            return True
    return any(
        value == text or fnmatchcase(value, text) for value in candidate.match_values
    )


def _candidate_filter_values(candidate: _Candidate, key: str) -> tuple[str, ...]:
    if key == "provider":
        return (candidate.target.provider,)
    if key == "scope":
        return (candidate.target.scope,) if candidate.target.scope is not None else ()
    if key == "adapter":
        return (candidate.target.adapter,)
    if key == "alias":
        return (candidate.alias.name,) if candidate.alias is not None else ()
    if key == "tag":
        return tuple(candidate.target.tags)
    if key == "streaming":
        return (_bool_filter_value(candidate.target.streaming),)
    if key in {"tools", "tool_call"}:
        return (_bool_filter_value(candidate.target.tools),)
    if key in {"available", "availability"}:
        return ("true",)
    info = candidate.info
    if info is None:
        return ()
    if key in {
        "reasoning",
        "temperature",
        "structured_output",
        "attachment",
        "open_weights",
    }:
        value = info.metadata.get(key)
        return (_bool_filter_value(value),) if isinstance(value, bool) else ()
    if key in {"family", "status"}:
        value = info.metadata.get(key)
        return (value,) if isinstance(value, str) and value else ()
    if key.startswith("modalities."):
        modalities = info.metadata.get("modalities")
        values = (
            modalities.get(key.partition(".")[2])
            if isinstance(modalities, Mapping)
            else None
        )
        return (
            tuple(item for item in values if isinstance(item, str))
            if isinstance(values, list | tuple)
            else ()
        )
    return ()


def _bool_filter_value(value: bool) -> str:
    return "true" if value else "false"


def _looks_exact_ref(selector: ModelSelector) -> bool:
    pattern = selector.pattern.strip()
    return "/" in pattern and not any(char in pattern for char in "*?[")


def _target_from_alias(
    alias: ModelAlias,
    *,
    providers: Mapping[str, CatalogProvider],
    models: Sequence[ModelInfo],
    envs: Mapping[str, str],
    strict: bool,
) -> ModelTarget | None:
    provider = providers.get(alias.provider)
    if provider is None:
        if strict:
            raise ToolangError(
                f"unknown model provider for alias {alias.name!r}: {alias.provider}"
            )
        return None
    missing = _missing_target_env_vars(provider, alias=alias, envs=envs)
    if missing:
        if strict:
            joined = ", ".join(missing)
            raise ToolangError(
                f"model alias {alias.name!r} is missing environment: {joined}"
            )
        return None
    if alias.provider == CUSTOM_MODEL_PROVIDER and not alias.endpoint:
        if strict:
            raise ToolangError(f"model alias {alias.name!r} is missing endpoint")
        return None
    info = _find_model_info_by_ref(
        models, provider=_provider_id(provider), ref=alias.ref
    )
    if info is not None:
        return _target_from_info(provider, info, envs=envs, alias=alias)
    return _target_from_alias_only(provider, alias, envs=envs)


def _find_model_info_by_ref(
    models: Sequence[ModelInfo],
    *,
    provider: str,
    ref: str,
) -> ModelInfo | None:
    for info in models:
        if info.provider == provider and info.ref == ref:
            return info
    return None


def _target_from_info(
    provider: CatalogProvider,
    info: ModelInfo,
    *,
    envs: Mapping[str, str],
    alias: ModelAlias | None = None,
) -> ModelTarget:
    request_options = _target_options(
        provider, dict(alias.options) if alias is not None else {}
    )
    reasoning = _reasoning_options(request_options)
    mode = _mode_option(request_options)
    _validate_reasoning_request(reasoning, info=info)
    mode_body, mode_headers = _model_mode_request(info, mode)
    mode_body.update(request_options)
    api_key_env = (
        alias.key_env
        if alias is not None and alias.key_env is not None
        else _provider_api_key_env(provider, envs=envs)
    )
    api_key = envs.get(api_key_env) if api_key_env else None
    model_endpoint = _model_provider_endpoint(info, envs=envs)
    endpoint = (
        alias.endpoint
        if alias is not None and alias.endpoint is not None
        else model_endpoint or default_provider_base_url(provider, environ=envs)
    )
    scope = _target_scope(provider, info=info, alias=alias, endpoint=endpoint)
    return ModelTarget(
        ref=info.ref,
        provider=_provider_id(provider),
        name=alias.display_name
        if alias is not None and alias.display_name is not None
        else info.name,
        model=alias.model
        if alias is not None and alias.model is not None
        else info.model,
        adapter=alias.adapter
        if alias is not None and alias.adapter is not None
        else info.adapter,
        base_url=endpoint,
        api_key=api_key,
        scope=scope,
        tags=alias.tags if alias is not None and alias.tags else info.tags,
        headers=_target_headers(
            provider,
            mode_headers,
            dict(alias.headers) if alias is not None else {},
        ),
        options=mode_body,
        tools=alias.tools
        if alias is not None and alias.tools is not None
        else info.tools,
        streaming=alias.streaming
        if alias is not None and alias.streaming is not None
        else info.streaming,
        catalog_revision=_metadata_text(info.metadata, "catalog_revision"),
        reasoning=reasoning,
        mode=mode,
    )


def _target_from_alias_only(
    provider: CatalogProvider,
    alias: ModelAlias,
    *,
    envs: Mapping[str, str],
) -> ModelTarget:
    model_name = alias.model or _provider_model_name_from_ref(alias.provider, alias.ref)
    endpoint = alias.endpoint or default_provider_base_url(provider, environ=envs)
    scope = alias.scope or _configured_scope(provider) or _scope_from_endpoint(endpoint)
    scope = scope or _provider_scope(alias.provider)
    api_key_env = alias.key_env or _provider_api_key_env(provider, envs=envs)
    request_options = _target_options(provider, dict(alias.options))
    reasoning = _reasoning_options(request_options)
    mode = _mode_option(request_options)
    return ModelTarget(
        ref=alias.ref,
        provider=_provider_id(provider),
        name=alias.display_name or model_name,
        model=model_name,
        adapter=alias.adapter or resolve_catalog_adapter(provider) or "unavailable",
        base_url=endpoint,
        api_key=envs.get(api_key_env) if api_key_env else None,
        scope=scope,
        tags=alias.tags,
        headers=_target_headers(provider, dict(alias.headers)),
        options=request_options,
        tools=True if alias.tools is None else alias.tools,
        streaming=True if alias.streaming is None else alias.streaming,
        reasoning=reasoning,
        mode=mode,
    )


def _provider_model_name_from_ref(provider: str, ref: str) -> str:
    if provider == "openrouter":
        return ref
    head, sep, tail = ref.partition("/")
    if sep:
        return tail.strip() or head.strip()
    return ref.strip()


def _default_provider_adapter(provider: str) -> str:
    if provider in {"deepseek", "google", "openrouter"}:
        return "chat_completions"
    return "responses"


def _provider_scope(provider: str) -> str:
    return "local" if provider in {"ollama", "llama_cpp"} else "remote"


def _scope_from_endpoint(endpoint: str | None) -> str | None:
    if endpoint is None:
        return None
    text = endpoint.strip().lower()
    if text.startswith(("http://127.0.0.1", "http://localhost", "http://[::1]")):
        return "local"
    if text.startswith(("http://", "https://")):
        return "remote"
    return None


def _missing_target_env_vars(
    provider: CatalogProvider,
    *,
    alias: ModelAlias | None,
    envs: Mapping[str, str],
) -> tuple[str, ...]:
    if isinstance(provider, Provider):
        if alias is not None and alias.key_env is not None:
            return () if str(envs.get(alias.key_env, "")).strip() else (alias.key_env,)
        return (
            ()
            if not provider.env
            or any(str(envs.get(name, "")).strip() for name in provider.env)
            else provider.env
        )
    required = list(required_provider_env_vars(provider))
    default_key_env = default_provider_api_key_env(provider)
    if alias is not None and alias.key_env is not None:
        required = [
            alias.key_env if name == default_key_env else name for name in required
        ]
        if default_key_env is None or alias.key_env not in required:
            required.append(alias.key_env)
    seen: set[str] = set()
    missing: list[str] = []
    for name in required:
        env_name = name.strip()
        if not env_name or env_name in seen:
            continue
        seen.add(env_name)
        if not str(envs.get(env_name, "")).strip():
            missing.append(env_name)
    return tuple(missing)


def _provider_id(provider: CatalogProvider) -> str:
    return provider.id if isinstance(provider, Provider) else provider.name


def _provider_api_key_env(
    provider: CatalogProvider,
    *,
    envs: Mapping[str, str],
) -> str | None:
    if isinstance(provider, Provider):
        return next(
            (name for name in provider.env if str(envs.get(name, "")).strip()),
            provider.env[0] if provider.env else None,
        )
    return default_provider_api_key_env(provider)


def _target_headers(
    provider: CatalogProvider,
    *overrides: Mapping[str, str],
) -> dict[str, str]:
    defaults: dict[str, str] = {}
    if _provider_id(provider) == "openrouter":
        defaults = {
            "HTTP-Referer": "https://toolang.ai",
            "X-OpenRouter-Title": "Toolang",
            "X-OpenRouter-Categories": "cli-agent",
        }
    lowered = {key.lower(): key for key in defaults}
    for values in overrides:
        for key, value in values.items():
            existing = lowered.get(key.lower())
            if existing is not None and existing != key:
                defaults.pop(existing, None)
            defaults[key] = value
            lowered[key.lower()] = key
    return defaults


def _target_options(
    provider: CatalogProvider,
    overrides: Mapping[str, object],
) -> dict[str, object]:
    options: dict[str, object] = {}
    if isinstance(provider, Provider):
        config = catalog_provider_config(provider)
        if config is not None:
            options.update(config.options)
    options.update(overrides)
    return options


def _reasoning_options(options: dict[str, object]) -> dict[str, object]:
    value = options.pop("reasoning", None)
    return dict(cast(Mapping[str, object], value)) if isinstance(value, Mapping) else {}


def _mode_option(options: dict[str, object]) -> str | None:
    value = options.pop("mode", None)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ToolangError("model mode must be non-empty text")
    return value.strip()


def _model_mode_request(
    info: ModelInfo,
    mode: str | None,
) -> tuple[dict[str, object], dict[str, str]]:
    if mode is None:
        return {}, {}
    experimental = info.metadata.get("experimental")
    modes = experimental.get("modes") if isinstance(experimental, Mapping) else None
    selected = modes.get(mode) if isinstance(modes, Mapping) else None
    if not isinstance(selected, Mapping):
        raise ToolangError(f"model {info.ref} does not advertise mode {mode!r}")
    provider = selected.get("provider")
    body = provider.get("body") if isinstance(provider, Mapping) else None
    headers = provider.get("headers") if isinstance(provider, Mapping) else None
    return (
        dict(body) if isinstance(body, Mapping) else {},
        {
            str(key): value
            for key, value in headers.items()
            if isinstance(key, str) and isinstance(value, str)
        }
        if isinstance(headers, Mapping)
        else {},
    )


def _model_provider_endpoint(
    info: ModelInfo,
    *,
    envs: Mapping[str, str],
) -> str | None:
    provider = info.metadata.get("provider")
    value = provider.get("api") if isinstance(provider, Mapping) else None
    if not isinstance(value, str) or not value.strip():
        return None
    return Template(value.strip()).safe_substitute(envs)


def _target_scope(
    provider: CatalogProvider,
    *,
    info: ModelInfo,
    alias: ModelAlias | None,
    endpoint: str | None,
) -> str:
    return (
        alias.scope
        if alias is not None and alias.scope is not None
        else _configured_scope(provider)
        or _scope_from_endpoint(endpoint)
        or info.scope
        or _provider_scope(_provider_id(provider))
    )


def _configured_scope(provider: CatalogProvider) -> str | None:
    if not isinstance(provider, Provider):
        return None
    config = catalog_provider_config(provider)
    return config.scope if config is not None else None


def _metadata_text(metadata: Mapping[str, object], key: str) -> str | None:
    value = metadata.get(key)
    return value if isinstance(value, str) and value else None


def _validate_reasoning_request(
    request: Mapping[str, object],
    *,
    info: ModelInfo,
) -> None:
    if not request:
        return
    raw_options = info.metadata.get("reasoning_options")
    options = (
        tuple(item for item in raw_options if isinstance(item, Mapping))
        if isinstance(raw_options, list | tuple)
        else ()
    )
    unknown = set(request) - {"enabled", "effort", "budget_tokens"}
    if unknown:
        joined = ", ".join(sorted(unknown))
        raise ToolangError(f"model {info.ref} has unknown reasoning controls: {joined}")
    enabled = request.get("enabled")
    if enabled is not None and (
        not isinstance(enabled, bool)
        or not any(option.get("type") == "toggle" for option in options)
    ):
        raise ToolangError(f"model {info.ref} does not advertise a reasoning toggle")
    effort = request.get("effort")
    if effort is not None:
        allowed = {
            value
            for option in options
            if option.get("type") == "effort"
            for value in option.get("values", ())
            if isinstance(value, str)
        }
        if not isinstance(effort, str) or effort not in allowed:
            joined = ", ".join(sorted(allowed)) or "none"
            raise ToolangError(
                f"model {info.ref} does not advertise reasoning effort "
                f"{effort!r} (allowed: {joined})"
            )
    budget = request.get("budget_tokens")
    if budget is not None:
        budget_options = tuple(
            option for option in options if option.get("type") == "budget_tokens"
        )
        if (
            isinstance(budget, bool)
            or not isinstance(budget, int)
            or budget < 0
            or not budget_options
        ):
            raise ToolangError(
                f"model {info.ref} does not advertise this reasoning token budget"
            )
        minimums = tuple(
            value
            for option in budget_options
            for value in (option.get("min"),)
            if isinstance(value, int) and not isinstance(value, bool)
        )
        if minimums and budget < min(minimums):
            raise ToolangError(
                f"model {info.ref} reasoning budget must be at least {min(minimums)}"
            )


def _candidate_match_values(
    selector: str, target: ModelTarget, *extra: str
) -> tuple[str, ...]:
    return _dedupe(
        (
            selector,
            target.ref,
            target.name,
            target.model,
            *extra,
        )
    )


def _require_allowed(
    target: ModelTarget,
    *,
    selector: str,
    allowed: Sequence[ModelTarget],
) -> None:
    if not allowed:
        return
    allowed_identities = {_target_identity(item) for item in allowed}
    if _target_identity(target) in allowed_identities:
        return
    allowed_text = ", ".join(f"{item.ref}[{item.provider}]" for item in allowed)
    raise ToolangError(
        f"model selector is outside the current resources: {selector} "
        f"(allowed: {allowed_text})"
    )


def _target_identity(target: ModelTarget) -> tuple[str, str, str, str | None]:
    return (target.ref, target.provider, target.model, target.base_url)


def _first_non_empty(*items: str | None) -> str:
    for item in items:
        if item is not None and item.strip():
            return item.strip()
    return DEFAULT_MODEL_SELECTOR


def _dedupe(items: Iterable[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    values: list[str] = []
    for item in items:
        text = item.strip()
        if not text or text in seen:
            continue
        seen.add(text)
        values.append(text)
    return tuple(values)


def _empty_model_selection_message(
    *,
    providers: Mapping[str, CatalogProvider],
    models: Sequence[ModelInfo],
    aliases: Mapping[str, ModelAlias],
    envs: Mapping[str, str],
) -> str:
    available = _discover_available_candidates(
        providers=providers,
        models=models,
        aliases=aliases,
        envs=envs,
    )
    if available:
        return NO_MATCHED_MODELS_MESSAGE
    return NO_AVAILABLE_MODELS_MESSAGE
