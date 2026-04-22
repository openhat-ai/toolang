"""Model selector resolution for execution."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Protocol

from toolang.base.error import ToolangError
from toolang.base.protocols.model import ModelProvider
from toolang.base.types.model import ModelInfo, ModelRoute, ModelTarget
from toolang.models.discovery import (
    default_provider_api_key_env,
    default_provider_base_url,
    missing_provider_env_vars,
    model_infos,
)

DEFAULT_MODEL_SELECTOR = "gpt-5"


class SupportsModelSelection(Protocol):
    """Minimal context shape needed to resolve one model selector."""

    model_providers: Mapping[str, ModelProvider]
    model_routes: Mapping[str, ModelRoute]
    default_models: tuple[str, ...]
    model_environ: Mapping[str, str]


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
        routes=context.model_routes,
        environ=context.model_environ,
    )
    if selector is not None and selector.strip():
        target = _resolve_one(
            selector.strip(),
            providers=context.model_providers,
            routes=context.model_routes,
            environ=context.model_environ,
        )
        _require_allowed(target, selector=selector.strip(), allowed=resolved_allowed)
        return target
    if default_selector is not None and default_selector.strip():
        target = _resolve_one(
            default_selector.strip(),
            providers=context.model_providers,
            routes=context.model_routes,
            environ=context.model_environ,
        )
        _require_allowed(target, selector=default_selector.strip(), allowed=resolved_allowed)
        return target
    if resolved_allowed:
        return resolved_allowed[0]
    for route_name in context.default_models:
        return _resolve_route(
            route_name,
            providers=context.model_providers,
            routes=context.model_routes,
            environ=context.model_environ,
        )
    return _resolve_one(
        DEFAULT_MODEL_SELECTOR,
        providers=context.model_providers,
        routes=context.model_routes,
        environ=context.model_environ,
    )


def select_model_selectors(
    context: SupportsModelSelection,
    *,
    thunk_selectors: Sequence[str] = (),
    activation_selectors: Sequence[str] = (),
    default_selector: str | None = None,
) -> tuple[str, ...]:
    """Return the effective ordered model selectors for one run."""

    resolved_thunk_refs = _resolve_selector_refs(
        thunk_selectors,
        providers=context.model_providers,
        routes=context.model_routes,
        environ=context.model_environ,
    )
    resolved_activation = _resolve_selector_targets(
        activation_selectors,
        providers=context.model_providers,
        routes=context.model_routes,
        environ=context.model_environ,
    )
    discovered_available = _discover_available_selector_targets(
        providers=context.model_providers,
        routes=context.model_routes,
        environ=context.model_environ,
    )

    if resolved_thunk_refs and resolved_activation:
        thunk_refs = {ref for _, ref in resolved_thunk_refs}
        selected = tuple(selector for selector, target in resolved_activation if target.ref in thunk_refs)
        if selected:
            return selected
        raise ToolangError("no compatible model between thunk model refs and activation --model options")

    if resolved_activation:
        return tuple(selector for selector, _target in resolved_activation)

    if resolved_thunk_refs:
        selected = _select_discovered_by_ref_order(
            refs=tuple(ref for _selector, ref in resolved_thunk_refs),
            candidates=discovered_available,
        )
        if selected:
            return selected
        resolved_thunk = _resolve_selector_targets(
            thunk_selectors,
            providers=context.model_providers,
            routes=context.model_routes,
            environ=context.model_environ,
        )
        return tuple(selector for selector, _target in resolved_thunk)

    if discovered_available:
        defaults = (
            (default_selector,)
            if default_selector and default_selector.strip()
            else tuple(context.default_models)
        ) or (DEFAULT_MODEL_SELECTOR,)
        preferred = _preferred_default_selectors(
            defaults,
            available=discovered_available,
            providers=context.model_providers,
            routes=context.model_routes,
            environ=context.model_environ,
        )
        ordered: list[str] = []
        seen: set[str] = set()
        for selector in (*preferred, *(selector for selector, _target in discovered_available)):
            if selector in seen:
                continue
            seen.add(selector)
            ordered.append(selector)
        if ordered:
            return tuple(ordered)

    defaults = (
        (default_selector,)
        if default_selector and default_selector.strip()
        else tuple(context.default_models)
    ) or (DEFAULT_MODEL_SELECTOR,)
    resolved_defaults = _resolve_selector_targets(
        defaults,
        providers=context.model_providers,
        routes=context.model_routes,
        environ=context.model_environ,
    )
    if resolved_defaults:
        return tuple(selector for selector, _target in resolved_defaults)
    raise ToolangError("no default model selector is available for this activation")


def _resolve_allowed_targets(
    selectors: Sequence[str] | None,
    *,
    providers: Mapping[str, ModelProvider],
    routes: Mapping[str, ModelRoute],
    environ: Mapping[str, str],
) -> tuple[ModelTarget, ...]:
    targets: list[ModelTarget] = []
    for raw in selectors or ():
        selector = raw.strip()
        if not selector:
            continue
        targets.append(
            _resolve_one(
                selector,
                providers=providers,
                routes=routes,
                environ=environ,
            )
        )
    return tuple(targets)


def _discover_available_selector_targets(
    *,
    providers: Mapping[str, ModelProvider],
    routes: Mapping[str, ModelRoute],
    environ: Mapping[str, str],
) -> tuple[tuple[str, ModelTarget], ...]:
    del routes
    targets: list[tuple[str, ModelTarget]] = []
    seen: set[str] = set()
    for provider in providers.values():
        if missing_provider_env_vars(provider, environ=environ):
            continue
        for info in model_infos(provider, environ=environ):
            selector = f"{info.ref}@{provider.name}"
            if selector in seen:
                continue
            seen.add(selector)
            targets.append((selector, _target_from_info(provider, info, environ=environ)))
    return tuple(targets)


def _resolve_selector_targets(
    selectors: Sequence[str] | None,
    *,
    providers: Mapping[str, ModelProvider],
    routes: Mapping[str, ModelRoute],
    environ: Mapping[str, str],
) -> tuple[tuple[str, ModelTarget], ...]:
    targets: list[tuple[str, ModelTarget]] = []
    seen: set[str] = set()
    for raw in selectors or ():
        selector = raw.strip()
        if not selector or selector in seen:
            continue
        seen.add(selector)
        targets.append(
            (
                selector,
                _resolve_one(
                    selector,
                    providers=providers,
                    routes=routes,
                    environ=environ,
                ),
            )
        )
    return tuple(targets)


def _resolve_selector_refs(
    selectors: Sequence[str] | None,
    *,
    providers: Mapping[str, ModelProvider],
    routes: Mapping[str, ModelRoute],
    environ: Mapping[str, str],
) -> tuple[tuple[str, str], ...]:
    refs: list[tuple[str, str]] = []
    seen: set[str] = set()
    for raw in selectors or ():
        selector = raw.strip()
        if not selector or selector in seen:
            continue
        seen.add(selector)
        if selector in routes:
            refs.append((selector, routes[selector].ref))
            continue
        raw_selector, explicit_provider = _split_provider_route(selector)
        if explicit_provider is not None:
            target = _resolve_one(
                selector,
                providers=providers,
                routes=routes,
                environ=environ,
            )
            refs.append((selector, target.ref))
            continue
        if "/" in raw_selector:
            exact_refs = {
                info.ref
                for provider in providers.values()
                if not missing_provider_env_vars(provider, environ=environ)
                for info in model_infos(provider, environ=environ)
                if info.ref == raw_selector
            }
            if exact_refs:
                refs.append((selector, next(iter(sorted(exact_refs)))))
                continue
        matched_refs = {
            info.ref
            for provider in providers.values()
            if not missing_provider_env_vars(provider, environ=environ)
            for info in _matching_model_infos(provider, raw_selector, environ=environ)
        }
        if not matched_refs:
            raise ToolangError(f"model selector could not be resolved: {selector}")
        if len(matched_refs) > 1:
            joined = ", ".join(sorted(matched_refs))
            raise ToolangError(
                f"model selector resolves to multiple refs: {selector} (matches {joined})"
            )
        refs.append((selector, next(iter(matched_refs))))
    return tuple(refs)


def _select_discovered_by_ref_order(
    *,
    refs: Sequence[str],
    candidates: Sequence[tuple[str, ModelTarget]],
) -> tuple[str, ...]:
    selected: list[str] = []
    seen: set[str] = set()
    for ref in refs:
        for selector, target in candidates:
            if target.ref != ref or selector in seen:
                continue
            seen.add(selector)
            selected.append(selector)
    return tuple(selected)


def _preferred_default_selectors(
    defaults: Sequence[str],
    *,
    available: Sequence[tuple[str, ModelTarget]],
    providers: Mapping[str, ModelProvider],
    routes: Mapping[str, ModelRoute],
    environ: Mapping[str, str],
) -> tuple[str, ...]:
    preferred: list[str] = []
    seen: set[str] = set()
    available_set = {selector for selector, _target in available}
    for raw in defaults:
        selector = raw.strip()
        if not selector:
            continue
        matches = _matching_available_selectors(
            selector,
            available=available_set,
            providers=providers,
            environ=environ,
        )
        if not matches:
            if selector in routes:
                matches = (selector,)
            else:
                try:
                    resolved = _resolve_selector_targets(
                        (selector,),
                        providers=providers,
                        routes=routes,
                        environ=environ,
                    )
                except ToolangError:
                    resolved = ()
                matches = tuple(item for item, _target in resolved)
        for match in matches:
            if match in seen:
                continue
            seen.add(match)
            preferred.append(match)
    return tuple(preferred)


def _matching_available_selectors(
    selector: str,
    *,
    available: set[str],
    providers: Mapping[str, ModelProvider],
    environ: Mapping[str, str],
) -> tuple[str, ...]:
    matches: list[str] = []
    seen: set[str] = set()
    text = selector.strip()
    if not text:
        return ()
    for provider in providers.values():
        if missing_provider_env_vars(provider, environ=environ):
            continue
        for info in _matching_model_infos(provider, text, environ=environ):
            explicit = f"{info.ref}@{provider.name}"
            if explicit not in available or explicit in seen:
                continue
            seen.add(explicit)
            matches.append(explicit)
    return tuple(matches)


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
    allowed_text = ", ".join(f"{item.ref}@{item.provider}" for item in allowed)
    raise ToolangError(
        f"model selector is not allowed for this activation: {selector} (allowed: {allowed_text})"
    )


def _target_identity(target: ModelTarget) -> tuple[str, str, str, str | None]:
    return (target.ref, target.provider, target.model, target.base_url)


def _split_provider_route(selector: str) -> tuple[str, str | None]:
    base, sep, provider_name = selector.partition("@")
    if not sep:
        return selector, None
    base = base.strip()
    provider_name = provider_name.strip()
    if not base or not provider_name:
        raise ToolangError(f"invalid model selector route: {selector}")
    return base, provider_name


def _require_provider(providers: Mapping[str, ModelProvider], name: str) -> ModelProvider:
    provider = providers.get(name)
    if provider is None:
        raise ToolangError(f"unknown model provider: {name}")
    return provider


def _resolve_one(
    selector: str,
    *,
    providers: Mapping[str, ModelProvider],
    routes: Mapping[str, ModelRoute],
    environ: Mapping[str, str],
) -> ModelTarget:
    if selector in routes:
        return _resolve_route(selector, providers=providers, routes=routes, environ=environ)
    raw_selector, explicit_provider = _split_provider_route(selector)
    if explicit_provider is not None:
        provider = _require_provider(providers, explicit_provider)
        target = _resolve_provider_selector(raw_selector, provider=provider, environ=environ)
        if target is None:
            raise ToolangError(f"model selector is not supported by {explicit_provider}: {raw_selector}")
        return target
    matches: list[ModelTarget] = []
    for provider in providers.values():
        target = _resolve_provider_selector(raw_selector, provider=provider, environ=environ)
        if target is not None:
            matches.append(target)
    if not matches:
        raise ToolangError(f"model selector could not be resolved: {selector}")
    if len(matches) > 1:
        provider_names = ", ".join(sorted(item.provider for item in matches))
        raise ToolangError(
            f"model selector is ambiguous: {selector} (matches {provider_names}); use <ref>@<provider> or a named model route"
        )
    return matches[0]


def _resolve_provider_selector(
    selector: str,
    *,
    provider: ModelProvider,
    environ: Mapping[str, str],
) -> ModelTarget | None:
    matches = _matching_model_infos(provider, selector, environ=environ)
    if not matches:
        return None
    if len(matches) > 1:
        refs = ", ".join(sorted({item.ref for item in matches}))
        raise ToolangError(
            f"model selector is ambiguous within {provider.name}: {selector} (matches {refs})"
        )
    return _target_from_info(provider, matches[0], environ=environ)


def _matching_model_infos(
    provider: ModelProvider,
    selector: str,
    *,
    environ: Mapping[str, str],
) -> tuple[ModelInfo, ...]:
    text = selector.strip()
    if not text:
        return ()
    exact = tuple(
        info
        for info in model_infos(provider, environ=environ)
        if text == info.ref
    )
    if exact:
        return exact
    return tuple(
        info
        for info in model_infos(provider, environ=environ)
        if text in info.selectors
    )


def _resolve_route(
    name: str,
    *,
    providers: Mapping[str, ModelProvider],
    routes: Mapping[str, ModelRoute],
    environ: Mapping[str, str],
) -> ModelTarget:
    route = routes.get(name)
    if route is None:
        raise ToolangError(f"model route not found: {name}")
    provider = _require_provider(providers, route.provider)
    info = _find_model_info_by_ref(provider, route.ref, environ=environ)
    if info is not None:
        return _target_from_info(provider, info, environ=environ, route=route)
    return _target_from_route(provider, route, environ=environ)


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
    route: ModelRoute | None = None,
) -> ModelTarget:
    api_key_env = route.api_key_env if route is not None and route.api_key_env is not None else default_provider_api_key_env(provider)
    api_key = environ.get(api_key_env) if api_key_env else None
    return ModelTarget(
        ref=info.ref,
        provider=provider.name,
        name=route.display_name if route is not None and route.display_name is not None else info.name,
        model=route.model if route is not None and route.model is not None else info.model,
        adapter=route.adapter if route is not None and route.adapter is not None else info.adapter,
        base_url=route.base_url if route is not None and route.base_url is not None else default_provider_base_url(provider, environ=environ),
        api_key=api_key,
        headers=dict(route.headers) if route is not None else {},
        options=dict(route.options) if route is not None else {},
        tools=route.tools if route is not None and route.tools is not None else info.tools,
        streaming=route.streaming if route is not None and route.streaming is not None else info.streaming,
    )


def _target_from_route(
    provider: ModelProvider,
    route: ModelRoute,
    *,
    environ: Mapping[str, str],
) -> ModelTarget:
    adapter = route.adapter
    if adapter is None:
        raise ToolangError(
            f"model route {route.name!r} must declare adapter when ref {route.ref!r} is not exposed by provider {provider.name!r}"
        )
    api_key_env = route.api_key_env if route.api_key_env is not None else default_provider_api_key_env(provider)
    api_key = environ.get(api_key_env) if api_key_env else None
    model_name = route.model or _model_name_from_ref(route.ref)
    return ModelTarget(
        ref=route.ref,
        provider=provider.name,
        name=route.display_name or model_name,
        model=model_name,
        adapter=adapter,
        base_url=route.base_url if route.base_url is not None else default_provider_base_url(provider, environ=environ),
        api_key=api_key,
        headers=dict(route.headers),
        options=dict(route.options),
        tools=True if route.tools is None else route.tools,
        streaming=True if route.streaming is None else route.streaming,
    )


def _model_name_from_ref(ref: str) -> str:
    head, sep, tail = ref.partition("/")
    if sep:
        return tail.strip() or head.strip()
    return ref.strip()
