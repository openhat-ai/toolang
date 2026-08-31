"""Resolve and narrow stable resources available to agent execution."""

from __future__ import annotations

from collections.abc import Mapping
from typing import cast

from toolang.base.protocols.tool import AgentTool
from toolang.base.types.model import ModelTarget
from toolang.base.types.policy import AgentCeiling
from toolang.common.errors import ToolangError
from toolang.common.query import SetOperator
from toolang.execution.types import (
    AgentCapResource,
    AgentResources,
    AgentToolResource,
)
from toolang.lang.ast import AgicDecl, Directive, FlowDecl
from toolang.plugin.models.collections import ModelCollection
from toolang.plugin.toolsets.loading import query_tools, validate_tool_queries
from toolang.plugin.toolsets.collections import tool_dataset
from toolang.plugin.toolsets.registry import (
    tool_ref_for_model_tool,
)
from toolang.setup import AgentSetup
from toolang.state.state import (
    AgentState,
    StatePublication,
    StateCap,
    state_module_caps,
)
from toolang.state.collections import cap_dataset
from toolang.state.types import EntryKind

_Runnable = AgicDecl | FlowDecl
_CAP_CEILING_FIELDS: tuple[tuple[EntryKind, str], ...] = (
    ("psyche", "psyches"),
    ("skill", "skills"),
    ("service", "services"),
    ("prompt", "prompts"),
)


def agent_model_targets(
    setup: AgentSetup,
    ceiling: AgentCeiling,
) -> tuple[str | None, tuple[tuple[str, ModelTarget], ...]]:
    """Return the default and selectable targets within one agent ceiling."""

    models = setup.models
    if ceiling.models is not None:
        models = models.match(ceiling.models) if ceiling.models else ModelCollection()
    targets = tuple((entry.ref, entry.target) for entry in models.entries)
    default = setup.defaults.model
    if default is not None and not models.contains(default):
        raise ToolangError("default model is outside the selectable model collection")
    return default, targets


def validate_agent_ceiling(
    setup: AgentSetup,
    state: AgentState | StatePublication,
    ceiling: AgentCeiling,
) -> None:
    """Validate one agent ceiling against immutable setup and state."""

    resolve_agent_resources(setup, state, ceiling)


def resolve_agent_resources(
    setup: AgentSetup,
    state: AgentState | StatePublication,
    ceiling: AgentCeiling,
    *,
    module: str | None = None,
) -> AgentResources:
    """Build initial stable resources from complete immutable snapshots."""

    models = setup.models
    if ceiling.models is not None:
        models = models.match(ceiling.models) if ceiling.models else ModelCollection()

    tools = setup.tools
    if ceiling.tools is not None:
        tools = tools.match(ceiling.tools) if ceiling.tools else type(tools)()

    caps = state_module_caps(state, module or "agent")
    caps = _apply_cap_ceiling(
        caps,
        ceiling,
        agent_name=setup.layout.name,
        label="cap",
    )
    return _agent_resources(models=models, tools=dict(tools), caps=caps)


def apply_agent_ceiling(
    setup: AgentSetup,
    state: AgentState | StatePublication,
    resources: AgentResources,
    ceiling: AgentCeiling,
    *,
    module: str | None = None,
) -> AgentResources:
    """Apply one agent ceiling without expanding the base resource set."""

    if ceiling.models is None:
        models = setup.models.subset(resources.models)
    elif not ceiling.models:
        models = ModelCollection()
    elif not resources.models:
        raise ToolangError("model ceiling matched no available models")
    else:
        models = setup.models.subset(resources.models).match(ceiling.models)

    available_tools = resource_tools(setup, resources)
    validate_tool_queries(dict(available_tools), ceiling.tools)
    tools = query_tools(dict(available_tools), ceiling.tools)

    caps = resource_caps(state, resources, module=module)
    caps = _apply_cap_ceiling(
        caps,
        ceiling,
        agent_name=setup.layout.name,
        label="cap ceiling",
    )
    return _agent_resources(models=models, tools=tools, caps=caps)


def _apply_cap_ceiling(
    caps: tuple[StateCap, ...],
    ceiling: AgentCeiling,
    *,
    agent_name: str,
    label: str,
) -> tuple[StateCap, ...]:
    selected_ids: set[tuple[str, str, str]] = set()
    for kind, field in _CAP_CEILING_FIELDS:
        entries = tuple(item for item in caps if item.kind == kind)
        queries = getattr(ceiling, field)
        if queries is None:
            selected = entries
        elif not queries:
            selected = ()
        else:
            dataset = cap_dataset(entries, agent_name=agent_name, kind=kind)
            dataset.require_each(queries, label=f"{label} {kind}")
            selected = tuple(
                cast(StateCap, view.record) for view in dataset.query(queries)
            )
        selected_ids.update((item.kind, item.name, item.ref) for item in selected)
    return tuple(
        item for item in caps if (item.kind, item.name, item.ref) in selected_ids
    )


def resolve_runnable_resources(
    selection: ModelCollection,
    *,
    runnable: _Runnable,
    base: AgentResources,
    setup: AgentSetup,
    state: AgentState | StatePublication,
    module: str | None = None,
) -> AgentResources:
    """Apply one runnable's authored queries within a chosen resource base."""

    model_directives = _directives(runnable, "models")
    if model_directives:
        if not base.models:
            raise ToolangError("run resources include no models")
        models = selection.subset(base.models).apply(
            _query_operations(model_directives)
        )
    else:
        models = selection.subset(base.models)

    available_tools = resource_tools(setup, base)
    selected_tools = tool_dataset(available_tools).apply(
        _query_operations(_directives(runnable, "tools"))
    )
    tools = {item.model_name: cast(AgentTool, item.record) for item in selected_tools}

    available_caps = resource_caps(state, base, module=module)
    selected_cap_ids: set[tuple[str, str, str]] = {
        (item.kind, item.name, item.ref)
        for item in available_caps
        if item.kind not in {"psyche", "skill", "service", "prompt"}
    }
    for kind, directive_name in (
        ("psyche", "psyches"),
        ("skill", "skills"),
        ("service", "services"),
        ("prompt", "prompts"),
    ):
        entries = tuple(item for item in available_caps if item.kind == kind)
        selected = cap_dataset(
            entries,
            agent_name=setup.layout.name,
            kind=kind,
        ).apply(_query_operations(_directives(runnable, directive_name)))
        selected_cap_ids.update(
            (item.kind, item.name, item.ref)
            for item in (cast(StateCap, view.record) for view in selected)
        )
    caps = tuple(
        item
        for item in available_caps
        if (item.kind, item.name, item.ref) in selected_cap_ids
    )
    return _agent_resources(models=models, tools=tools, caps=caps)


def validate_model_binding(
    selection: ModelCollection,
    *,
    runnable: _Runnable,
    resources: AgentResources,
    model: str | None,
) -> None:
    """Validate one bound model within final runnable resources."""

    if model is not None:
        entry = selection.resolve(model)
        if entry.key not in resources.models:
            raise ToolangError(f"model ref is outside run resources: {model}")
    elif isinstance(runnable, AgicDecl):
        raise ToolangError(f"run requires a model: {runnable.name}")


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
    state: AgentState | StatePublication,
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
) -> ModelCollection:
    """Return the immutable model collection captured in Setup."""

    return setup.models


def _agent_resources(
    *,
    models: ModelCollection,
    tools: Mapping[str, AgentTool],
    caps: tuple[StateCap, ...],
) -> AgentResources:
    return AgentResources(
        models=models.keys(),
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


def _query_operations(
    directives: tuple[Directive, ...],
) -> tuple[tuple[SetOperator, tuple[str, ...]], ...]:
    return tuple(
        (
            cast(SetOperator, directive.operator),
            tuple(value for value in directive.values if value),
        )
        for directive in directives
    )
