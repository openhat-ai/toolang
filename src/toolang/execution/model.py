"""Model selector resolution for execution."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from toolang.base.error import ToolangError
from toolang.base.protocols.model import ModelPlugin
from toolang.base.types.model import ModelBinding, ResolvedModel

DEFAULT_MODEL_SELECTOR = "gpt-5"


@dataclass(frozen=True, slots=True)
class ModelProfile:
    """One named model profile loaded from config."""

    ref: str
    plugin: str
    model: str
    base_url: str | None = None
    api_key_env: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    options: dict[str, Any] = field(default_factory=dict)


class SupportsModelResolution(Protocol):
    """Minimal context shape needed to resolve one model selector."""

    model_plugins: Mapping[str, ModelPlugin]
    model_profiles: Mapping[str, ModelProfile]
    default_models: tuple[str, ...]
    model_environ: Mapping[str, str]


def resolve_model(
    context: SupportsModelResolution,
    *,
    selector: str | None,
    default_selector: str | None = None,
    allowed_selectors: Sequence[str] | None = None,
) -> ModelBinding:
    """Resolve one model selector against one uptime context."""

    resolved_allowed = _resolve_allowed_bindings(
        allowed_selectors,
        plugins=context.model_plugins,
        profiles=context.model_profiles,
        environ=context.model_environ,
    )
    if selector is not None and selector.strip():
        binding = _resolve_one(
            selector.strip(),
            plugins=context.model_plugins,
            profiles=context.model_profiles,
            environ=context.model_environ,
        )
        _require_allowed(binding, selector=selector.strip(), allowed=resolved_allowed)
        return binding
    if default_selector is not None and default_selector.strip():
        binding = _resolve_one(
            default_selector.strip(),
            plugins=context.model_plugins,
            profiles=context.model_profiles,
            environ=context.model_environ,
        )
        _require_allowed(binding, selector=default_selector.strip(), allowed=resolved_allowed)
        return binding
    if resolved_allowed:
        return resolved_allowed[0]
    for profile_name in context.default_models:
        return _resolve_profile(
            profile_name,
            plugins=context.model_plugins,
            profiles=context.model_profiles,
            environ=context.model_environ,
        )
    return _resolve_one(
        DEFAULT_MODEL_SELECTOR,
        plugins=context.model_plugins,
        profiles=context.model_profiles,
        environ=context.model_environ,
    )


def select_model_selectors(
    context: SupportsModelResolution,
    *,
    thunk_selectors: Sequence[str] = (),
    activation_selectors: Sequence[str] = (),
    default_selector: str | None = None,
) -> tuple[str, ...]:
    """Return the effective ordered model selectors for one run."""

    resolved_thunk = _resolve_selector_bindings(
        thunk_selectors,
        plugins=context.model_plugins,
        profiles=context.model_profiles,
        environ=context.model_environ,
    )
    resolved_activation = _resolve_selector_bindings(
        activation_selectors,
        plugins=context.model_plugins,
        profiles=context.model_profiles,
        environ=context.model_environ,
    )

    if resolved_thunk and resolved_activation:
        thunk_ids = {_binding_identity(binding) for _, binding in resolved_thunk}
        selected = tuple(
            selector
            for selector, binding in resolved_activation
            if _binding_identity(binding) in thunk_ids
        )
        if selected:
            return selected
        raise ToolangError("no compatible model between thunk model directive and activation --model options")

    if resolved_activation:
        return tuple(selector for selector, _binding in resolved_activation)

    if resolved_thunk:
        return tuple(selector for selector, _binding in resolved_thunk)

    defaults = ((default_selector,) if default_selector and default_selector.strip() else tuple(context.default_models)) or (
        DEFAULT_MODEL_SELECTOR,
    )
    resolved_defaults = _resolve_selector_bindings(
        defaults,
        plugins=context.model_plugins,
        profiles=context.model_profiles,
        environ=context.model_environ,
    )
    if resolved_defaults:
        return tuple(selector for selector, _binding in resolved_defaults)
    raise ToolangError("no default model selector is available for this activation")


def _resolve_allowed_bindings(
    selectors: Sequence[str] | None,
    *,
    plugins: Mapping[str, ModelPlugin],
    profiles: Mapping[str, ModelProfile],
    environ: Mapping[str, str],
) -> tuple[ModelBinding, ...]:
    bindings: list[ModelBinding] = []
    for raw in selectors or ():
        selector = raw.strip()
        if not selector:
            continue
        bindings.append(
            _resolve_one(
                selector,
                plugins=plugins,
                profiles=profiles,
                environ=environ,
            )
        )
    return tuple(bindings)


def _resolve_selector_bindings(
    selectors: Sequence[str] | None,
    *,
    plugins: Mapping[str, ModelPlugin],
    profiles: Mapping[str, ModelProfile],
    environ: Mapping[str, str],
) -> tuple[tuple[str, ModelBinding], ...]:
    bindings: list[tuple[str, ModelBinding]] = []
    seen: set[str] = set()
    for raw in selectors or ():
        selector = raw.strip()
        if not selector or selector in seen:
            continue
        seen.add(selector)
        bindings.append(
            (
                selector,
                _resolve_one(
                    selector,
                    plugins=plugins,
                    profiles=profiles,
                    environ=environ,
                ),
            )
        )
    return tuple(bindings)


def _require_allowed(
    binding: ModelBinding,
    *,
    selector: str,
    allowed: Sequence[ModelBinding],
) -> None:
    if not allowed:
        return
    allowed_identities = {_binding_identity(item) for item in allowed}
    if _binding_identity(binding) in allowed_identities:
        return
    allowed_text = ", ".join(item.target.ref for item in allowed)
    raise ToolangError(
        f"model selector is not allowed for this activation: {selector} (allowed: {allowed_text})"
    )


def _binding_identity(binding: ModelBinding) -> tuple[str, str, str, str | None]:
    target = binding.target
    return (target.ref, target.plugin, target.model, target.base_url)


def _split_plugin_route(selector: str) -> tuple[str, str | None]:
    base, sep, plugin_name = selector.partition("@")
    if not sep:
        return selector, None
    base = base.strip()
    plugin_name = plugin_name.strip()
    if not base or not plugin_name:
        raise ToolangError(f"invalid model selector route: {selector}")
    return base, plugin_name


def _require_plugin(plugins: Mapping[str, ModelPlugin], name: str) -> ModelPlugin:
    plugin = plugins.get(name)
    if plugin is None:
        raise ToolangError(f"unknown model plugin: {name}")
    return plugin


def _resolve_one(
    selector: str,
    *,
    plugins: Mapping[str, ModelPlugin],
    profiles: Mapping[str, ModelProfile],
    environ: Mapping[str, str],
) -> ModelBinding:
    if selector in profiles:
        return _resolve_profile(selector, plugins=plugins, profiles=profiles, environ=environ)
    raw_selector, explicit_plugin = _split_plugin_route(selector)
    if explicit_plugin is not None:
        plugin = _require_plugin(plugins, explicit_plugin)
        target = plugin.resolve_selector(raw_selector, environ=environ)
        if target is None:
            raise ToolangError(f"model selector is not supported by {explicit_plugin}: {raw_selector}")
        return ModelBinding(target=target, plugin=plugin)
    matches: list[ModelBinding] = []
    for plugin in plugins.values():
        target = plugin.resolve_selector(raw_selector, environ=environ)
        if target is None:
            continue
        matches.append(ModelBinding(target=target, plugin=plugin))
    if not matches:
        raise ToolangError(f"model selector could not be resolved: {selector}")
    if len(matches) > 1:
        plugin_names = ", ".join(sorted(item.plugin.name for item in matches))
        raise ToolangError(
            f"model selector is ambiguous: {selector} (matches {plugin_names}); use <ref>@<plugin> or a named model profile"
        )
    return matches[0]


def _resolve_profile(
    name: str,
    *,
    plugins: Mapping[str, ModelPlugin],
    profiles: Mapping[str, ModelProfile],
    environ: Mapping[str, str],
) -> ModelBinding:
    profile = profiles.get(name)
    if profile is None:
        raise ToolangError(f"model profile not found: {name}")
    plugin = _require_plugin(plugins, profile.plugin)
    api_key = environ.get(profile.api_key_env) if profile.api_key_env is not None else None
    return ModelBinding(
        target=ResolvedModel(
            ref=profile.ref,
            plugin=profile.plugin,
            model=profile.model,
            base_url=profile.base_url,
            api_key=api_key,
            headers=dict(profile.headers),
            options=dict(profile.options),
        ),
        plugin=plugin,
    )
