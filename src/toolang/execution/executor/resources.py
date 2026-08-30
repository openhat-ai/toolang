"""Resolve and narrow stable resources available to agent execution."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol, cast

from toolang.base.protocols.tool import AgentTool
from toolang.base.types.model import ModelAlias, ModelInfo, ModelTarget, Provider
from toolang.base.types.policy import AgentCeiling
from toolang.common.errors import ToolangError
from toolang.common.selectors import SelectorOperator, apply_selector_operations
from toolang.execution.types import (
    AgentCapResource,
    AgentResources,
    AgentToolResource,
)
from toolang.lang.ast import AgicDecl, Directive, FlowDecl
from toolang.plugin.models.config import (
    ProviderConfig,
    parse_default_models,
    parse_model_aliases,
)
from toolang.plugin.models.messages import NO_AVAILABLE_MODELS_MESSAGE
from toolang.plugin.models.resolution import (
    resolve_model,
    resolve_model_ref,
    select_model_selectors,
    selectable_model_targets,
)
from toolang.plugin.toolsets.loading import select_tools, validate_tool_selectors
from toolang.plugin.toolsets.registry import (
    selected_tool_names,
    tool_ref_for_model_tool,
)
from toolang.setup import AgentSetup
from toolang.state.state import (
    AgentState,
    StateCap,
    select_cap_entries,
    state_module_caps,
)

_Runnable = AgicDecl | FlowDecl


class _ModelSelection(Protocol):
    providers: Mapping[str, Provider]
    models: tuple[ModelInfo, ...]
    model_aliases: Mapping[str, ModelAlias]
    default_models: tuple[str, ...]
    envs: Mapping[str, str]
    provider_configs: Mapping[str, ProviderConfig]


@dataclass(frozen=True, slots=True)
class _SnapshotModelSelection:
    providers: Mapping[str, Provider]
    models: tuple[ModelInfo, ...]
    model_aliases: Mapping[str, ModelAlias]
    default_models: tuple[str, ...]
    envs: Mapping[str, str]
    provider_configs: Mapping[str, ProviderConfig]


def agent_model_targets(
    setup: AgentSetup,
    state: AgentState,
    ceiling: AgentCeiling,
) -> tuple[str | None, tuple[tuple[str, ModelTarget], ...]]:
    """Return the default and selectable targets within one agent ceiling."""

    selection = _snapshot_model_selection(setup, state)
    selectors = _select_agent_model_selectors(selection, ceiling)
    targets = (
        selectable_model_targets(
            providers=setup.providers,
            models=setup.models,
            aliases=selection.model_aliases,
            envs=setup.envs,
            provider_configs=selection.provider_configs,
            selectors=selectors,
        )
        if selectors
        else ()
    )
    default = (
        resolve_model_ref(
            selection,
            selector=setup.bindings.model,
            default_selector=selectors[0],
            allowed_selectors=selectors,
        )
        if selectors
        else None
    )
    return default, targets


def validate_agent_ceiling(
    setup: AgentSetup,
    state: AgentState,
    ceiling: AgentCeiling,
) -> None:
    """Validate one agent ceiling against immutable setup and state."""

    resolve_agent_resources(setup, state, ceiling)


def resolve_agent_resources(
    setup: AgentSetup,
    state: AgentState,
    ceiling: AgentCeiling,
    *,
    module: str | None = None,
) -> AgentResources:
    """Build initial stable resources from complete immutable snapshots."""

    selection = _snapshot_model_selection(setup, state)
    models = _select_agent_model_selectors(selection, ceiling)

    validate_tool_selectors(dict(setup.tools), ceiling.tools)
    tools = select_tools(dict(setup.tools), ceiling.tools)

    caps = state_module_caps(state, module or "agent")
    if ceiling.caps is not None:
        missing = [
            selector
            for selector in ceiling.caps
            if not select_cap_entries(
                caps,
                (selector,),
                agent_name=setup.layout.name,
            )
        ]
        if missing:
            raise ToolangError(f"cap selector matched no caps: {', '.join(missing)}")
        caps = (
            select_cap_entries(
                caps,
                ceiling.caps,
                agent_name=setup.layout.name,
            )
            if ceiling.caps
            else ()
        )
    return _agent_resources(models=models, tools=tools, caps=caps)


def apply_agent_ceiling(
    setup: AgentSetup,
    state: AgentState,
    resources: AgentResources,
    ceiling: AgentCeiling,
    *,
    module: str | None = None,
) -> AgentResources:
    """Apply one agent ceiling without expanding the base resource set."""

    selection = _snapshot_model_selection(setup, state)
    if ceiling.models is None:
        models = resources.models
    elif not ceiling.models:
        models = ()
    elif not resources.models:
        raise ToolangError("model ceiling matched no available models")
    else:
        models = select_model_selectors(
            selection,
            directive_selectors=ceiling.models,
            allowed_selectors=resources.models,
        )

    available_tools = resource_tools(setup, resources)
    validate_tool_selectors(dict(available_tools), ceiling.tools)
    tools = select_tools(dict(available_tools), ceiling.tools)

    caps = resource_caps(state, resources, module=module)
    if ceiling.caps is not None:
        missing = [
            selector
            for selector in ceiling.caps
            if not select_cap_entries(
                caps,
                (selector,),
                agent_name=setup.layout.name,
            )
        ]
        if missing:
            raise ToolangError(
                "cap ceiling matched no available caps: " + ", ".join(missing)
            )
        caps = (
            select_cap_entries(
                caps,
                ceiling.caps,
                agent_name=setup.layout.name,
            )
            if ceiling.caps
            else ()
        )
    return _agent_resources(models=models, tools=tools, caps=caps)


def resolve_runnable_resources(
    selection: _ModelSelection,
    *,
    runnable: _Runnable,
    base: AgentResources,
    setup: AgentSetup,
    state: AgentState,
    module: str | None = None,
) -> AgentResources:
    """Apply one runnable's authored selectors within a chosen resource base."""

    model_directives = _directives(runnable, "models")
    if model_directives:
        if not base.models:
            raise ToolangError("run resources include no models")
        selected = tuple(
            value
            for directive in model_directives
            for value in directive.values
            if value
        )
        models = select_model_selectors(
            selection,
            directive_selectors=selected,
            allowed_selectors=base.models,
        )
    else:
        models = base.models

    available_tools = resource_tools(setup, base)
    tool_names = apply_selector_operations(
        tuple(available_tools),
        _selector_operations(_directives(runnable, "tools")),
        lambda values: selected_tool_names(
            {
                name: tool_ref_for_model_tool(name, available_tools[name])
                for name in available_tools
            },
            values,
        ),
    )
    tools = {name: available_tools[name] for name in tool_names}

    available_caps = resource_caps(state, base, module=module)
    selected_cap_ids: set[tuple[str, str, str]] = {
        (item.kind, item.name, item.ref)
        for item in available_caps
        if item.kind not in {"psyche", "skill", "service"}
    }
    for kind, directive_name in (
        ("psyche", "psyches"),
        ("skill", "skills"),
        ("service", "services"),
    ):
        entries = tuple(item for item in available_caps if item.kind == kind)
        selected_cap_ids.update(
            (item.kind, item.name, item.ref)
            for item in apply_selector_operations(
                entries,
                _selector_operations(_directives(runnable, directive_name)),
                lambda values, entries=entries, kind=kind: select_cap_entries(
                    entries,
                    values,
                    agent_name=setup.layout.name,
                    implicit_kind=kind,
                ),
                identity=lambda item: (item.kind, item.name, item.ref),
            )
        )
    caps = tuple(
        item
        for item in available_caps
        if (item.kind, item.name, item.ref) in selected_cap_ids
    )
    return _agent_resources(models=models, tools=tools, caps=caps)


def validate_model_binding(
    selection: _ModelSelection,
    *,
    runnable: _Runnable,
    resources: AgentResources,
    model: str | None,
) -> None:
    """Validate one bound model within final runnable resources."""

    if model is not None or isinstance(runnable, AgicDecl):
        resolve_model(
            selection,
            selector=model,
            default_selector=resources.models[0] if resources.models else None,
            allowed_selectors=resources.models,
        )


def resource_tools(
    setup: AgentSetup,
    resources: AgentResources,
) -> Mapping[str, AgentTool]:
    """Resolve stable tool identities against the current immutable setup."""

    result: dict[str, AgentTool] = {}
    for item in resources.tools:
        tool = setup.tools.get(item.model_name)
        if tool is None:
            raise ToolangError(f"run tool resource is unavailable: {item.model_name}")
        ref = tool_ref_for_model_tool(item.model_name, tool)
        if (ref.plugin, ref.toolset, ref.name) != (
            item.plugin,
            item.toolset,
            item.name,
        ):
            raise ToolangError(f"run tool resource changed: {item.model_name}")
        result[item.model_name] = tool
    return result


def resource_caps(
    state: AgentState,
    resources: AgentResources,
    *,
    module: str | None = None,
) -> tuple[StateCap, ...]:
    """Resolve stable cap identities against the current immutable state."""

    by_id: dict[tuple[str, str, str], StateCap] = {
        (item.kind, item.name, item.ref): item
        for item in state_module_caps(state, module or "agent")
    }
    result: list[StateCap] = []
    for item in resources.caps:
        cap = by_id.get((item.kind, item.name, item.ref))
        if cap is None:
            raise ToolangError(
                f"run cap resource is unavailable: {item.kind}/{item.name}"
            )
        result.append(cap)
    return tuple(result)


def snapshot_model_selection(
    setup: AgentSetup,
    state: AgentState,
) -> _ModelSelection:
    """Return model selection facts for immutable setup and state snapshots."""

    return _snapshot_model_selection(setup, state)


def _agent_resources(
    *,
    models: tuple[str, ...],
    tools: Mapping[str, AgentTool],
    caps: tuple[StateCap, ...],
) -> AgentResources:
    return AgentResources(
        models=models,
        tools=tuple(
            AgentToolResource(
                model_name=model_name,
                plugin=ref.plugin,
                toolset=ref.toolset,
                name=ref.name,
            )
            for model_name, tool in tools.items()
            for ref in (tool_ref_for_model_tool(model_name, tool),)
        ),
        caps=tuple(
            AgentCapResource(kind=item.kind, name=item.name, ref=item.ref)
            for item in caps
        ),
    )


def _directives(
    runnable: _Runnable,
    name: str,
) -> tuple[Directive, ...]:
    return tuple(item for item in runnable.directives if item.name == name)


def _selector_operations(
    directives: tuple[Directive, ...],
) -> tuple[tuple[SelectorOperator, tuple[str, ...]], ...]:
    return tuple(
        (
            cast(SelectorOperator, directive.operator),
            tuple(value for value in directive.values if value),
        )
        for directive in directives
    )


def _snapshot_model_selection(
    setup: AgentSetup,
    state: AgentState,
) -> _SnapshotModelSelection:
    layers = (state.root_config, state.home_config)
    return _SnapshotModelSelection(
        providers=setup.providers,
        models=setup.models,
        model_aliases=parse_model_aliases(layers),
        default_models=parse_default_models(layers),
        envs=setup.envs,
        provider_configs=cast(
            Mapping[str, ProviderConfig],
            setup.provider_configs,
        ),
    )


def _select_agent_model_selectors(
    selection: _ModelSelection,
    ceiling: AgentCeiling,
) -> tuple[str, ...]:
    if ceiling.models == ():
        return ()
    if ceiling.models is None:
        try:
            return select_model_selectors(selection)
        except ToolangError as exc:
            if str(exc) == NO_AVAILABLE_MODELS_MESSAGE:
                return ()
            raise
    for selector in ceiling.models:
        select_model_selectors(
            selection,
            allowed_selectors=(selector,),
        )
    return select_model_selectors(
        selection,
        allowed_selectors=ceiling.models,
    )
