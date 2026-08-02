"""Agent-configured and executor-resolved resource ceilings."""

from __future__ import annotations

from collections.abc import Callable, Hashable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Protocol, TypeVar

from toolang.base.protocols.model import ModelProvider
from toolang.base.protocols.tool import AgentTool
from toolang.base.types.model import ModelAlias, ModelInfo, ModelTarget
from toolang.common.errors import ToolangError
from toolang.lang.ast import AgicDecl, Directive, FlowDecl
from toolang.plugin.models.config import parse_default_models, parse_model_aliases
from toolang.plugin.models.messages import NO_AVAILABLE_MODELS_MESSAGE
from toolang.plugin.models.resolution import (
    resolve_model,
    select_model_selectors,
    selectable_model_targets,
)
from toolang.plugin.tools.loading import select_tools, validate_tool_selectors
from toolang.plugin.tools.registry import selected_tool_names, tool_ref_for_model_tool
from toolang.setup import AgentSetup
from toolang.state.state import AgentState, PreparedCap, select_cap_entries

_Executable = AgicDecl | FlowDecl
_T = TypeVar("_T")


@dataclass(frozen=True, slots=True)
class CeilingSpec:
    """Stable selector lists used to resolve one execution-tree ceiling."""

    models: tuple[str, ...] | None = None
    tools: tuple[str, ...] | None = None
    caps: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "models", _normalize(self.models, name="model"))
        object.__setattr__(self, "tools", _normalize(self.tools, name="tool"))
        object.__setattr__(self, "caps", _normalize(self.caps, name="cap"))


@dataclass(frozen=True, slots=True)
class _AgentCeiling:
    """Concrete absolute resources available to one execution tree."""

    models: tuple[str, ...]
    tools: Mapping[str, AgentTool]
    caps: tuple[PreparedCap, ...]


@dataclass(frozen=True, slots=True)
class _RunCeiling:
    """Concrete resources available to one agic or flow run."""

    models: tuple[str, ...]
    tools: Mapping[str, AgentTool]
    caps: tuple[PreparedCap, ...]


class _ModelSelection(Protocol):
    providers: Mapping[str, ModelProvider]
    models: tuple[ModelInfo, ...]
    model_aliases: Mapping[str, ModelAlias]
    default_models: tuple[str, ...]
    envs: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class _SnapshotModelSelection:
    providers: Mapping[str, ModelProvider]
    models: tuple[ModelInfo, ...]
    model_aliases: Mapping[str, ModelAlias]
    default_models: tuple[str, ...]
    envs: Mapping[str, str]


def agent_model_targets(
    setup: AgentSetup,
    state: AgentState,
    spec: CeilingSpec,
) -> tuple[str | None, tuple[tuple[str, ModelTarget], ...]]:
    """Return the default and selectable targets within one agent ceiling."""

    selection = _snapshot_model_selection(setup, state)
    selectors = _select_agent_model_selectors(selection, spec)
    targets = (
        selectable_model_targets(
            providers=setup.providers,
            models=setup.models,
            aliases=selection.model_aliases,
            envs=setup.envs,
            selectors=selectors,
        )
        if selectors
        else ()
    )
    return (selectors[0] if selectors else None), targets


def validate_ceiling_spec(
    setup: AgentSetup,
    state: AgentState,
    spec: CeilingSpec,
) -> None:
    """Validate one ceiling spec without exposing its resolved form."""

    resolve_agent_ceiling(setup, state, spec)


def resolve_agent_ceiling(
    setup: AgentSetup,
    state: AgentState,
    spec: CeilingSpec,
) -> _AgentCeiling:
    """Resolve one ceiling spec against complete immutable snapshots."""

    selection = _snapshot_model_selection(setup, state)
    models = _select_agent_model_selectors(selection, spec)

    validate_tool_selectors(dict(setup.tools), spec.tools)
    tools = select_tools(dict(setup.tools), spec.tools)

    caps = tuple(state.caps)
    if spec.caps is not None:
        missing = [
            selector
            for selector in spec.caps
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
                spec.caps,
                agent_name=setup.layout.name,
            )
            if spec.caps
            else ()
        )
    return _AgentCeiling(
        models,
        MappingProxyType(dict(tools)),
        caps,
    )


def validate_root_run_resources(
    setup: AgentSetup,
    state: AgentState,
    *,
    executable: _Executable,
    agent: _AgentCeiling,
    model: str | None,
) -> None:
    """Validate root runnable directives and model selection before acceptance."""

    selection = _snapshot_model_selection(setup, state)
    ceiling = resolve_run_ceiling(
        selection,
        executable=executable,
        agent=agent,
        flow=None,
        agent_name=setup.layout.name,
    )
    if model is not None or isinstance(executable, AgicDecl):
        resolve_model(
            selection,
            selector=model,
            default_selector=ceiling.models[0] if ceiling.models else None,
            allowed_selectors=ceiling.models,
        )


def resolve_run_ceiling(
    selection: _ModelSelection,
    *,
    executable: _Executable,
    agent: _AgentCeiling,
    flow: _RunCeiling | None,
    agent_name: str,
) -> _RunCeiling:
    """Resolve one run, resetting flows and locally narrowing agics."""

    base = agent if isinstance(executable, FlowDecl) else flow or agent
    model_directives = _directives(executable, "models")
    if model_directives:
        if not base.models:
            raise ToolangError("run ceiling allows no models")
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

    tools = _select_values(
        tuple(base.tools),
        _directives(executable, "tools"),
        lambda values: selected_tool_names(
            {
                name: tool_ref_for_model_tool(name, base.tools[name])
                for name in base.tools
            },
            values,
        ),
    )
    selected_tools = {name: base.tools[name] for name in tools}

    selected_cap_ids: set[tuple[str, str, str]] = {
        (item.kind, item.name, item.ref)
        for item in base.caps
        if item.kind not in {"psyche", "skill", "service"}
    }
    for kind, directive_name in (
        ("psyche", "psyches"),
        ("skill", "skills"),
        ("service", "services"),
    ):
        entries = tuple(item for item in base.caps if item.kind == kind)
        selected_cap_ids.update(
            (item.kind, item.name, item.ref)
            for item in _select_values(
                entries,
                _directives(executable, directive_name),
                lambda values, entries=entries, kind=kind: select_cap_entries(
                    entries,
                    values,
                    agent_name=agent_name,
                    implicit_kind=kind,
                ),
                identity=lambda item: (item.kind, item.name, item.ref),
            )
        )
    caps = tuple(
        item
        for item in base.caps
        if (item.kind, item.name, item.ref) in selected_cap_ids
    )
    return _RunCeiling(
        models,
        MappingProxyType(selected_tools),
        caps,
    )


def _directives(
    executable: _Executable,
    name: str,
) -> tuple[Directive, ...]:
    return tuple(item for item in executable.directives if item.name == name)


def _select_values(
    base: tuple[_T, ...],
    directives: tuple[Directive, ...],
    match: Callable[[tuple[str, ...]], Sequence[_T]],
    *,
    identity: Callable[[_T], Hashable] = lambda item: item,
) -> tuple[_T, ...]:
    inherited: list[_T] = []
    seen: set[Hashable] = set()
    for item in base:
        key = identity(item)
        if key not in seen:
            inherited.append(item)
            seen.add(key)
    current = list(inherited)
    for directive in directives:
        matches = list(match(tuple(value for value in directive.values if value)))
        if directive.operator == "=":
            allowed = {identity(item) for item in matches}
            current = [item for item in current if identity(item) in allowed]
        elif directive.operator == "+=":
            seen = {identity(item) for item in current}
            for item in matches:
                key = identity(item)
                if key not in seen:
                    current.append(item)
                    seen.add(key)
        elif directive.operator == "-=":
            blocked = {identity(item) for item in matches}
            current = [item for item in current if identity(item) not in blocked]
    return tuple(current)


def _normalize(
    values: tuple[str, ...] | None,
    *,
    name: str,
) -> tuple[str, ...] | None:
    if values is None:
        return None
    normalized: list[str] = []
    for value in values:
        text = value.strip()
        if not text:
            raise ValueError(f"{name} ceiling selectors must not be empty")
        if text not in normalized:
            normalized.append(text)
    return tuple(normalized)


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
    )


def _select_agent_model_selectors(
    selection: _ModelSelection,
    spec: CeilingSpec,
) -> tuple[str, ...]:
    if spec.models == ():
        return ()
    if spec.models is None:
        try:
            return select_model_selectors(selection)
        except ToolangError as exc:
            if str(exc) == NO_AVAILABLE_MODELS_MESSAGE:
                return ()
            raise
    for selector in spec.models:
        select_model_selectors(
            selection,
            allowed_selectors=(selector,),
        )
    return select_model_selectors(
        selection,
        allowed_selectors=spec.models,
    )
