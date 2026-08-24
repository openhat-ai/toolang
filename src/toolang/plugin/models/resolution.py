"""Model selector and target resolution."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from typing import Protocol, cast

from toolang.base.errors import ToolangError
from toolang.base.types.model import Model, ModelAlias, ModelInfo, ModelTarget, Provider
from toolang.plugin.models.config import ProviderConfig
from toolang.plugin.models.messages import (
    NO_AVAILABLE_MODELS_MESSAGE,
    NO_MATCHED_MODELS_MESSAGE,
)
from toolang.plugin.models.provider_resolver import (
    env_is_ready,
    selected_credential_value,
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


def split_model_selectors(items: list[str] | tuple[str, ...] | None) -> tuple[str, ...]:
    """Split repeated and CSV model selector inputs."""

    return split_selector_list(items)


def parse_model_selector(raw: str) -> ModelSelector:
    """Parse one model selector."""

    return parse_selector(raw, domain="model")


def resolve_catalog_adapter(
    provider: Provider,
    *,
    model: Model | None = None,
) -> str | None:
    """Resolve a protocol adapter from explicit config and catalog signals."""

    if model is not None and model.resolved is not None:
        return model.resolved.adapter
    if provider.resolved is None:
        raise RuntimeError(f"provider {provider.id!r} has not been resolved")
    return provider.resolved.adapter


def catalog_model_api(
    provider: Provider,
    model: Model,
    *,
    envs: Mapping[str, str],
) -> str | None:
    """Return the resolved API base for one model record."""

    del envs
    if model.resolved is not None:
        return model.resolved.api
    if provider.resolved is None:
        raise RuntimeError(f"provider {provider.id!r} has not been resolved")
    return provider.resolved.api


class SupportsModelSelection(Protocol):
    """Minimal context shape needed to resolve model selectors."""

    providers: Mapping[str, Provider]
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

    providers: Mapping[str, Provider]
    models: tuple[ModelInfo, ...]
    model_aliases: Mapping[str, ModelAlias]
    default_models: tuple[str, ...]
    envs: Mapping[str, str]
    provider_configs: Mapping[str, ProviderConfig] = field(default_factory=dict)

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
            provider_configs=self.provider_configs,
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

    provider_configs = _context_provider_configs(context)
    resolved_allowed = _resolve_allowed_targets(
        allowed_selectors,
        providers=context.providers,
        models=context.models,
        aliases=context.model_aliases,
        envs=context.envs,
        provider_configs=provider_configs,
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
        provider_configs=provider_configs,
    )
    if not matches:
        raise ToolangError(
            _empty_model_selection_message(
                providers=context.providers,
                models=context.models,
                aliases=context.model_aliases,
                envs=context.envs,
                provider_configs=provider_configs,
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

    provider_configs = _context_provider_configs(context)
    directive_candidates = _resolve_selector_targets(
        directive_selectors,
        providers=context.providers,
        models=context.models,
        aliases=context.model_aliases,
        envs=context.envs,
        provider_configs=provider_configs,
    )
    allowed_candidates = _resolve_selector_targets(
        allowed_selectors,
        providers=context.providers,
        models=context.models,
        aliases=context.model_aliases,
        envs=context.envs,
        provider_configs=provider_configs,
    )
    if directive_selectors and not directive_candidates:
        raise ToolangError(
            _empty_model_selection_message(
                providers=context.providers,
                models=context.models,
                aliases=context.model_aliases,
                envs=context.envs,
                provider_configs=provider_configs,
            )
        )
    if allowed_selectors and not allowed_candidates:
        raise ToolangError(
            _empty_model_selection_message(
                providers=context.providers,
                models=context.models,
                aliases=context.model_aliases,
                envs=context.envs,
                provider_configs=provider_configs,
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
        provider_configs=provider_configs,
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
        provider_configs=provider_configs,
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
            provider_configs=provider_configs,
        )
    )


def selectable_model_targets(
    *,
    providers: Mapping[str, Provider],
    models: Sequence[ModelInfo],
    aliases: Mapping[str, ModelAlias],
    envs: Mapping[str, str],
    provider_configs: Mapping[str, ProviderConfig] | None = None,
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
                provider_configs=provider_configs or {},
            )
        )
    return tuple(
        (candidate.selector, candidate.target)
        for candidate in _discover_available_candidates(
            providers=providers,
            models=models,
            aliases=aliases,
            envs=envs,
            provider_configs=provider_configs or {},
        )
    )


def _resolve_allowed_targets(
    selectors: Sequence[str] | None,
    *,
    providers: Mapping[str, Provider],
    models: Sequence[ModelInfo],
    aliases: Mapping[str, ModelAlias],
    envs: Mapping[str, str],
    provider_configs: Mapping[str, ProviderConfig],
) -> tuple[ModelTarget, ...]:
    targets: list[ModelTarget] = []
    for candidate in _resolve_selector_targets(
        selectors,
        providers=providers,
        models=models,
        aliases=aliases,
        envs=envs,
        provider_configs=provider_configs,
    ):
        targets.append(candidate.target)
    return tuple(targets)


def _discover_available_candidates(
    *,
    providers: Mapping[str, Provider],
    models: Sequence[ModelInfo],
    aliases: Mapping[str, ModelAlias],
    envs: Mapping[str, str],
    provider_configs: Mapping[str, ProviderConfig],
) -> tuple[_Candidate, ...]:
    candidates: list[_Candidate] = []
    seen: set[tuple[str, str, str, str | None]] = set()
    for name, alias in aliases.items():
        target = _target_from_alias(
            alias,
            providers=providers,
            models=models,
            envs=envs,
            provider_configs=provider_configs,
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
        if not _model_info_ready(info):
            continue
        target = _target_from_info(
            provider,
            info,
            envs=envs,
            provider_configs=provider_configs,
        )
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
    providers: Mapping[str, Provider],
    models: Sequence[ModelInfo],
    aliases: Mapping[str, ModelAlias],
    envs: Mapping[str, str],
    provider_configs: Mapping[str, ProviderConfig],
) -> tuple[_Candidate, ...]:
    candidates = _discover_available_candidates(
        providers=providers,
        models=models,
        aliases=aliases,
        envs=envs,
        provider_configs=provider_configs,
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
                provider_configs=provider_configs,
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
                provider_configs=provider_configs,
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
    providers: Mapping[str, Provider],
    models: Sequence[ModelInfo],
    aliases: Mapping[str, ModelAlias],
    envs: Mapping[str, str],
    provider_configs: Mapping[str, ProviderConfig],
) -> tuple[_Candidate, ...]:
    matches: list[_Candidate] = []
    for info in models:
        if info.ref != ref:
            continue
        provider = providers.get(info.provider)
        if provider is None or _provider_id(provider) == CUSTOM_MODEL_PROVIDER:
            continue
        if not _model_info_ready(info):
            continue
        target = _target_from_info(
            provider,
            info,
            envs=envs,
            provider_configs=provider_configs,
        )
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
            provider_configs=provider_configs,
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


def _model_info_ready(info: ModelInfo) -> bool:
    value = info.metadata.get("resolved_ready")
    return value if isinstance(value, bool) else info.adapter != "unavailable"


def _looks_exact_ref(selector: ModelSelector) -> bool:
    pattern = selector.pattern.strip()
    return "/" in pattern and not any(char in pattern for char in "*?[")


def _target_from_alias(
    alias: ModelAlias,
    *,
    providers: Mapping[str, Provider],
    models: Sequence[ModelInfo],
    envs: Mapping[str, str],
    provider_configs: Mapping[str, ProviderConfig],
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
        return _target_from_info(
            provider,
            info,
            envs=envs,
            provider_configs=provider_configs,
            alias=alias,
        )
    return _target_from_alias_only(
        provider,
        alias,
        envs=envs,
        provider_configs=provider_configs,
    )


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
    provider: Provider,
    info: ModelInfo,
    *,
    envs: Mapping[str, str],
    provider_configs: Mapping[str, ProviderConfig],
    alias: ModelAlias | None = None,
) -> ModelTarget:
    config = provider_configs.get(provider.id)
    request_options = _target_options(
        config, dict(alias.options) if alias is not None else {}
    )
    reasoning = _reasoning_options(request_options)
    mode = _mode_option(request_options)
    _validate_reasoning_request(reasoning, info=info)
    mode_body, mode_headers = _model_mode_request(info, mode)
    mode_body.update(request_options)
    resolved = provider.resolved
    if resolved is None:
        raise RuntimeError(f"provider {provider.id!r} has not been resolved")
    api_key = (
        envs.get(alias.key_env)
        if alias is not None and alias.key_env is not None
        else selected_credential_value(provider, environ=envs)
    )
    endpoint = (
        alias.endpoint
        if alias is not None and alias.endpoint is not None
        else _metadata_text(info.metadata, "resolved_api")
    )
    scope = _target_scope(
        provider,
        info=info,
        alias=alias,
        endpoint=endpoint,
        config=config,
    )
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
        catalog=_metadata_text(info.metadata, "catalog"),
        catalog_revision=_metadata_text(info.metadata, "catalog_revision"),
        reasoning=reasoning,
        mode=mode,
    )


def _target_from_alias_only(
    provider: Provider,
    alias: ModelAlias,
    *,
    envs: Mapping[str, str],
    provider_configs: Mapping[str, ProviderConfig],
) -> ModelTarget:
    model_name = alias.model or _provider_model_name_from_ref(alias.provider, alias.ref)
    resolved = provider.resolved
    if resolved is None:
        raise RuntimeError(f"provider {provider.id!r} has not been resolved")
    endpoint = alias.endpoint or resolved.api
    config = provider_configs.get(provider.id)
    scope = alias.scope or _configured_scope(config) or _scope_from_endpoint(endpoint)
    scope = scope or _provider_scope(alias.provider)
    request_options = _target_options(config, dict(alias.options))
    reasoning = _reasoning_options(request_options)
    mode = _mode_option(request_options)
    return ModelTarget(
        ref=alias.ref,
        provider=_provider_id(provider),
        name=alias.display_name or model_name,
        model=model_name,
        adapter=alias.adapter or resolve_catalog_adapter(provider) or "unavailable",
        base_url=endpoint,
        api_key=(
            envs.get(alias.key_env)
            if alias.key_env is not None
            else selected_credential_value(provider, environ=envs)
        ),
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
    provider: Provider,
    *,
    alias: ModelAlias | None,
    envs: Mapping[str, str],
) -> tuple[str, ...]:
    if alias is not None and alias.key_env is not None:
        return () if str(envs.get(alias.key_env, "")).strip() else (alias.key_env,)
    resolved = provider.resolved
    if resolved is None:
        raise RuntimeError(f"provider {provider.id!r} has not been resolved")
    if env_is_ready(resolved.env, environ=envs):
        return ()
    return tuple(
        dict.fromkeys(
            name
            for alternative in resolved.env
            for name in (
                (alternative,) if isinstance(alternative, str) else alternative
            )
            if not str(envs.get(name, "")).strip()
        )
    )


def _provider_id(provider: Provider) -> str:
    return provider.id


def _target_headers(
    provider: Provider,
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
    config: ProviderConfig | None,
    overrides: Mapping[str, object],
) -> dict[str, object]:
    options: dict[str, object] = {}
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


def _target_scope(
    provider: Provider,
    *,
    info: ModelInfo,
    alias: ModelAlias | None,
    endpoint: str | None,
    config: ProviderConfig | None,
) -> str:
    return (
        alias.scope
        if alias is not None and alias.scope is not None
        else _configured_scope(config)
        or _scope_from_endpoint(endpoint)
        or info.scope
        or _provider_scope(_provider_id(provider))
    )


def _configured_scope(config: ProviderConfig | None) -> str | None:
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
    providers: Mapping[str, Provider],
    models: Sequence[ModelInfo],
    aliases: Mapping[str, ModelAlias],
    envs: Mapping[str, str],
    provider_configs: Mapping[str, ProviderConfig],
) -> str:
    available = _discover_available_candidates(
        providers=providers,
        models=models,
        aliases=aliases,
        envs=envs,
        provider_configs=provider_configs,
    )
    if available:
        return NO_MATCHED_MODELS_MESSAGE
    return NO_AVAILABLE_MODELS_MESSAGE


def _context_provider_configs(
    context: SupportsModelSelection,
) -> Mapping[str, ProviderConfig]:
    value = getattr(context, "provider_configs", {})
    if not isinstance(value, Mapping):
        return {}
    return {
        str(name): config
        for name, config in value.items()
        if isinstance(config, ProviderConfig)
    }
