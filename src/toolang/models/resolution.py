"""Model selector and target resolution."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from fnmatch import fnmatchcase
from typing import Protocol

from toolang.base.error import ToolangError
from toolang.base.protocols.model import ModelProvider
from toolang.base.types.model import ModelAlias, ModelInfo, ModelTarget
from toolang.models.discovery import (
    default_provider_api_key_env,
    default_provider_base_url,
    missing_provider_env_vars,
    model_infos,
    required_provider_env_vars,
)
from toolang.models.errors import NO_AVAILABLE_MODELS_MESSAGE, NO_MATCHED_MODELS_MESSAGE
from toolang.selectors import (
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


class SupportsModelSelection(Protocol):
    """Minimal context shape needed to resolve model selectors."""

    model_providers: Mapping[str, ModelProvider]
    model_aliases: Mapping[str, ModelAlias]
    default_models: tuple[str, ...]
    model_environ: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class _Candidate:
    selector: str
    target: ModelTarget
    match_values: tuple[str, ...]
    alias: ModelAlias | None = None


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
        providers=context.model_providers,
        aliases=context.model_aliases,
        environ=context.model_environ,
    )
    effective_selector = _first_non_empty(
        selector,
        default_selector,
        *context.default_models,
        DEFAULT_MODEL_SELECTOR,
    )
    matches = _resolve_selector_targets(
        (effective_selector,),
        providers=context.model_providers,
        aliases=context.model_aliases,
        environ=context.model_environ,
    )
    if not matches:
        raise ToolangError(
            _empty_model_selection_message(
                providers=context.model_providers,
                aliases=context.model_aliases,
                environ=context.model_environ,
            )
        )
    if len(matches) > 1:
        joined = ", ".join(item.selector for item in matches)
        raise ToolangError(f"model selector is ambiguous: {effective_selector} (matches {joined})")
    target = matches[0].target
    _require_allowed(target, selector=effective_selector, allowed=resolved_allowed)
    return target


def select_model_selectors(
    context: SupportsModelSelection,
    *,
    thunk_selectors: Sequence[str] = (),
    activation_selectors: Sequence[str] = (),
    default_selector: str | None = None,
) -> tuple[str, ...]:
    """Return the effective ordered model selectors for one run."""

    thunk_candidates = _resolve_selector_targets(
        thunk_selectors,
        providers=context.model_providers,
        aliases=context.model_aliases,
        environ=context.model_environ,
    )
    activation_candidates = _resolve_selector_targets(
        activation_selectors,
        providers=context.model_providers,
        aliases=context.model_aliases,
        environ=context.model_environ,
    )
    if thunk_selectors and not thunk_candidates:
        raise ToolangError(
            _empty_model_selection_message(
                providers=context.model_providers,
                aliases=context.model_aliases,
                environ=context.model_environ,
            )
        )
    if activation_selectors and not activation_candidates:
        raise ToolangError(
            _empty_model_selection_message(
                providers=context.model_providers,
                aliases=context.model_aliases,
                environ=context.model_environ,
            )
        )
    if thunk_candidates and activation_candidates:
        thunk_identities = {_target_identity(candidate.target) for candidate in thunk_candidates}
        selected = tuple(
            candidate.selector
            for candidate in activation_candidates
            if _target_identity(candidate.target) in thunk_identities
        )
        if selected:
            return selected
        raise ToolangError(NO_MATCHED_MODELS_MESSAGE)

    if activation_candidates:
        return _dedupe(candidate.selector for candidate in activation_candidates)

    if thunk_candidates:
        return _dedupe(candidate.selector for candidate in thunk_candidates)

    available = _discover_available_candidates(
        providers=context.model_providers,
        aliases=context.model_aliases,
        environ=context.model_environ,
    )
    defaults = (
        (default_selector,)
        if default_selector and default_selector.strip()
        else tuple(context.default_models)
    ) or (DEFAULT_MODEL_SELECTOR,)
    preferred = _resolve_selector_targets(
        defaults,
        providers=context.model_providers,
        aliases=context.model_aliases,
        environ=context.model_environ,
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
            providers=context.model_providers,
            aliases=context.model_aliases,
            environ=context.model_environ,
        )
    )


def selectable_model_targets(
    *,
    providers: Mapping[str, ModelProvider],
    aliases: Mapping[str, ModelAlias],
    environ: Mapping[str, str],
    selectors: Sequence[str] | None = None,
) -> tuple[tuple[str, ModelTarget], ...]:
    """Return selectable model targets for CLI/API listing."""

    if selectors:
        return tuple(
            (candidate.selector, candidate.target)
            for candidate in _resolve_selector_targets(
                selectors,
                providers=providers,
                aliases=aliases,
                environ=environ,
            )
        )
    return tuple(
        (candidate.selector, candidate.target)
        for candidate in _discover_available_candidates(
            providers=providers,
            aliases=aliases,
            environ=environ,
        )
    )


def _resolve_allowed_targets(
    selectors: Sequence[str] | None,
    *,
    providers: Mapping[str, ModelProvider],
    aliases: Mapping[str, ModelAlias],
    environ: Mapping[str, str],
) -> tuple[ModelTarget, ...]:
    targets: list[ModelTarget] = []
    for candidate in _resolve_selector_targets(
        selectors,
        providers=providers,
        aliases=aliases,
        environ=environ,
    ):
        targets.append(candidate.target)
    return tuple(targets)


def _discover_available_candidates(
    *,
    providers: Mapping[str, ModelProvider],
    aliases: Mapping[str, ModelAlias],
    environ: Mapping[str, str],
) -> tuple[_Candidate, ...]:
    candidates: list[_Candidate] = []
    seen: set[tuple[str, str, str, str | None]] = set()
    for name, alias in aliases.items():
        target = _target_from_alias(alias, providers=providers, environ=environ, strict=False)
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
    for provider in providers.values():
        if provider.name == CUSTOM_MODEL_PROVIDER or missing_provider_env_vars(provider, environ=environ):
            continue
        for info in model_infos(provider, environ=environ):
            target = _target_from_info(provider, info, environ=environ)
            identity = _target_identity(target)
            if identity in seen:
                continue
            seen.add(identity)
            selector = f"{info.ref}[{provider.name}]"
            candidates.append(
                _Candidate(
                    selector=selector,
                    target=target,
                    match_values=_candidate_match_values(selector, target, *info.selectors),
                )
            )
    return tuple(candidates)


def _resolve_selector_targets(
    selectors: Sequence[str] | None,
    *,
    providers: Mapping[str, ModelProvider],
    aliases: Mapping[str, ModelAlias],
    environ: Mapping[str, str],
) -> tuple[_Candidate, ...]:
    candidates = _discover_available_candidates(providers=providers, aliases=aliases, environ=environ)
    selected: list[_Candidate] = []
    seen: set[tuple[str, str, str, str | None]] = set()
    for raw in selectors or ():
        text = raw.strip()
        if not text:
            continue
        if text in aliases:
            target = _target_from_alias(aliases[text], providers=providers, environ=environ, strict=True)
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
        matches = tuple(candidate for candidate in candidates if _candidate_matches(candidate, selector))
        if not matches and _looks_exact_ref(selector):
            matches = _resolve_exact_ref(selector.pattern, providers=providers, aliases=aliases, environ=environ)
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
    providers: Mapping[str, ModelProvider],
    aliases: Mapping[str, ModelAlias],
    environ: Mapping[str, str],
) -> tuple[_Candidate, ...]:
    matches: list[_Candidate] = []
    for provider in providers.values():
        if provider.name == CUSTOM_MODEL_PROVIDER or missing_provider_env_vars(provider, environ=environ):
            continue
        target = _target_from_provider_ref(provider, ref, environ=environ)
        if target is None:
            continue
        matches.append(
            _Candidate(
                selector=f"{target.ref}[{provider.name}]",
                target=target,
                match_values=_candidate_match_values(ref, target),
            )
        )
    for name, alias in aliases.items():
        if alias.ref != ref:
            continue
        target = _target_from_alias(alias, providers=providers, environ=environ, strict=False)
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
        if not actual_values or not any(filter_value_matches(actual, values) for actual in actual_values):
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
    return any(value == text or fnmatchcase(value, text) for value in candidate.match_values)


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
    if key == "tools":
        return (_bool_filter_value(candidate.target.tools),)
    return ()


def _bool_filter_value(value: bool) -> str:
    return "true" if value else "false"


def _looks_exact_ref(selector: ModelSelector) -> bool:
    pattern = selector.pattern.strip()
    return "/" in pattern and not any(char in pattern for char in "*?[")


def _target_from_provider_ref(
    provider: ModelProvider,
    ref: str,
    *,
    environ: Mapping[str, str],
) -> ModelTarget | None:
    info = _find_model_info_by_ref(provider, ref, environ=environ)
    if info is not None:
        return _target_from_info(provider, info, environ=environ)
    return None


def _target_from_alias(
    alias: ModelAlias,
    *,
    providers: Mapping[str, ModelProvider],
    environ: Mapping[str, str],
    strict: bool,
) -> ModelTarget | None:
    provider = providers.get(alias.provider)
    if provider is None:
        if strict:
            raise ToolangError(f"unknown model provider for alias {alias.name!r}: {alias.provider}")
        return None
    missing = _missing_target_env_vars(provider, alias=alias, environ=environ)
    if missing:
        if strict:
            joined = ", ".join(missing)
            raise ToolangError(f"model alias {alias.name!r} is missing environment: {joined}")
        return None
    if alias.provider == CUSTOM_MODEL_PROVIDER and not alias.endpoint:
        if strict:
            raise ToolangError(f"model alias {alias.name!r} is missing endpoint")
        return None
    info = _find_model_info_by_ref(provider, alias.ref, environ=environ)
    if info is not None:
        return _target_from_info(provider, info, environ=environ, alias=alias)
    return _target_from_alias_only(provider, alias, environ=environ)


def _find_model_info_by_ref(
    provider: ModelProvider,
    ref: str,
    *,
    environ: Mapping[str, str],
) -> ModelInfo | None:
    for info in model_infos(provider, environ=environ):
        if info.ref == ref:
            return info
    return None


def _target_from_info(
    provider: ModelProvider,
    info: ModelInfo,
    *,
    environ: Mapping[str, str],
    alias: ModelAlias | None = None,
) -> ModelTarget:
    api_key_env = alias.key_env if alias is not None and alias.key_env is not None else default_provider_api_key_env(provider)
    api_key = environ.get(api_key_env) if api_key_env else None
    scope = alias.scope if alias is not None and alias.scope is not None else info.scope or _provider_scope(provider.name)
    return ModelTarget(
        ref=info.ref,
        provider=provider.name,
        name=alias.display_name if alias is not None and alias.display_name is not None else info.name,
        model=alias.model if alias is not None and alias.model is not None else info.model,
        adapter=alias.adapter if alias is not None and alias.adapter is not None else info.adapter,
        base_url=alias.endpoint if alias is not None and alias.endpoint is not None else default_provider_base_url(provider, environ=environ),
        api_key=api_key,
        scope=scope,
        tags=alias.tags if alias is not None and alias.tags else info.tags,
        headers=dict(alias.headers) if alias is not None else {},
        options=dict(alias.options) if alias is not None else {},
        tools=alias.tools if alias is not None and alias.tools is not None else info.tools,
        streaming=alias.streaming if alias is not None and alias.streaming is not None else info.streaming,
    )


def _target_from_alias_only(
    provider: ModelProvider,
    alias: ModelAlias,
    *,
    environ: Mapping[str, str],
) -> ModelTarget:
    model_name = alias.model or _provider_model_name_from_ref(alias.provider, alias.ref)
    endpoint = alias.endpoint or default_provider_base_url(provider, environ=environ)
    scope = alias.scope or _scope_from_endpoint(endpoint) or _provider_scope(alias.provider)
    api_key_env = alias.key_env or default_provider_api_key_env(provider)
    return ModelTarget(
        ref=alias.ref,
        provider=provider.name,
        name=alias.display_name or model_name,
        model=model_name,
        adapter=alias.adapter or _default_provider_adapter(provider.name),
        base_url=endpoint,
        api_key=environ.get(api_key_env) if api_key_env else None,
        scope=scope,
        tags=alias.tags,
        headers=dict(alias.headers),
        options=dict(alias.options),
        tools=True if alias.tools is None else alias.tools,
        streaming=True if alias.streaming is None else alias.streaming,
    )


def _provider_model_name_from_ref(provider: str, ref: str) -> str:
    if provider == "openrouter":
        return ref
    head, sep, tail = ref.partition("/")
    if sep:
        return tail.strip() or head.strip()
    return ref.strip()


def _default_provider_adapter(provider: str) -> str:
    del provider
    return "responses"


def _provider_scope(provider: str) -> str:
    return "local" if provider == "ollama" else "remote"


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
    provider: ModelProvider,
    *,
    alias: ModelAlias | None,
    environ: Mapping[str, str],
) -> tuple[str, ...]:
    required = list(required_provider_env_vars(provider))
    default_key_env = default_provider_api_key_env(provider)
    if alias is not None and alias.key_env is not None:
        required = [alias.key_env if name == default_key_env else name for name in required]
        if default_key_env is None or alias.key_env not in required:
            required.append(alias.key_env)
    seen: set[str] = set()
    missing: list[str] = []
    for name in required:
        env_name = name.strip()
        if not env_name or env_name in seen:
            continue
        seen.add(env_name)
        if not str(environ.get(env_name, "")).strip():
            missing.append(env_name)
    return tuple(missing)


def _candidate_match_values(selector: str, target: ModelTarget, *extra: str) -> tuple[str, ...]:
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
        f"model selector is not allowed for this activation: {selector} (allowed: {allowed_text})"
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
    providers: Mapping[str, ModelProvider],
    aliases: Mapping[str, ModelAlias],
    environ: Mapping[str, str],
) -> str:
    available = _discover_available_candidates(
        providers=providers,
        aliases=aliases,
        environ=environ,
    )
    if available:
        return NO_MATCHED_MODELS_MESSAGE
    return NO_AVAILABLE_MODELS_MESSAGE
