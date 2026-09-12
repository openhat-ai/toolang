"""Runnable declaration lookup shared by binding and execution."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, TypeAlias, cast

from toolang.base.errors import ToolangError
from toolang.lang.ast import (
    AgicDecl,
    FlowDecl,
    Program,
    StructDecl,
)
from toolang.lang.types import parse_public_runnable_ref
from toolang.state.state import (
    AgentState,
    effective_agics,
    state_program,
)
from toolang.state.runnable_collections import runnable_dataset

Runnable: TypeAlias = AgicDecl | FlowDecl
RUNNABLE_DOCUMENTATION_MAX_CHARS = 512
_BUILTIN_TYPES = frozenset(
    {
        "Text",
        "Number",
        "Boolean",
        "Json",
        "Part",
        "TextPart",
        "ImagePart",
        "AudioPart",
        "DocumentPart",
        "ToolCallPart",
        "ToolResultPart",
    }
)


@dataclass(frozen=True, slots=True)
class ResolvedRunnable:
    """One public runnable resolved to its owning State module."""

    name: str
    module: str
    executable: Runnable

    @property
    def ref(self) -> str:
        """Return the kind-qualified public runnable reference."""

        return f"{self.executable.kind}:{self.name}"

    @property
    def qualified(self) -> str:
        """Return the stable State-local runnable identity."""

        return f"{self.module}${self.ref}"


RouteAction: TypeAlias = Literal["run", "execute"]


@dataclass(frozen=True, slots=True)
class RunnableRoute:
    """One currently resolved public target and its allowed model actions."""

    runnable: ResolvedRunnable
    actions: tuple[RouteAction, ...]


@dataclass(frozen=True, slots=True)
class AgicRoutes:
    """Authored Agic routing authority and State-resolved model hints."""

    hands: tuple[str, ...] = ()
    handoffs: tuple[str, ...] = ()
    resolved: tuple[RunnableRoute, ...] = ()

    def allows(self, action: RouteAction, target: ResolvedRunnable) -> bool:
        """Return whether authored routing authority permits one target."""

        return any(
            route.runnable.ref == target.ref and action in route.actions
            for route in self.resolved
        )


def resolve_public_runnable(
    state: AgentState,
    name: str,
    *,
    kind: str | None = None,
) -> ResolvedRunnable:
    """Resolve one public runnable with its effective name and owner module."""

    module, executable = resolve_state_runnable(state, name, kind=kind)
    return ResolvedRunnable(name=name, module=module, executable=executable)


def resolve_agic_routes(state: AgentState, agic: AgicDecl) -> AgicRoutes:
    """Resolve one Agic's authored routes against a captured State."""

    hands = _directive_values(agic, "hands")
    handoffs = _directive_values(agic, "handoffs")
    actions_by_ref: dict[str, set[RouteAction]] = {}
    groups: tuple[tuple[RouteAction, tuple[str, ...]], ...] = (
        ("run", hands),
        ("execute", handoffs),
    )
    dataset = runnable_dataset(state)
    for route_action, queries in groups:
        selected = dataset.query(queries) if queries else ()
        for item in selected:
            target = ResolvedRunnable(
                name=item.name,
                module=item.module,
                executable=cast(Runnable, item.record),
            )
            actions_by_ref.setdefault(target.ref, set()).add(route_action)
    resolved = tuple(
        RunnableRoute(
            runnable=ResolvedRunnable(
                name=item.name,
                module=item.module,
                executable=cast(Runnable, item.record),
            ),
            actions=tuple(action for action in ("run", "execute") if action in actions),
        )
        for item in dataset.items
        if (actions := actions_by_ref.get(f"{item.kind}:{item.name}")) is not None
    )
    return AgicRoutes(hands=hands, handoffs=handoffs, resolved=resolved)


def _directive_values(agic: AgicDecl, name: str) -> tuple[str, ...]:
    directive = next((item for item in agic.directives if item.name == name), None)
    return directive.values if directive is not None else ()


def resolve_runnable(
    program: Program,
    name: str,
    *,
    kind: str | None = None,
) -> Runnable:
    """Resolve one unique runnable and optionally require its declaration kind."""

    if not name or name != name.strip():
        raise ValueError("run spec requires a canonical runnable name")
    matches: tuple[Runnable, ...] = (
        *(agic for agic in effective_agics(program) if agic.name == name),
        *(flow for flow in program.flows if flow.name == name),
    )
    if kind is not None:
        matches = tuple(item for item in matches if item.kind == kind)
    if not matches:
        raise ToolangError(f"Runnable not found: {name}")
    if len(matches) > 1:
        raise ToolangError(f"Runnable name is not unique: {name}")
    return matches[0]


def resolve_state_runnable(
    state: AgentState,
    name: str,
    *,
    kind: str | None = None,
) -> tuple[str, Runnable]:
    """Resolve one public runnable to its owning module and declaration."""

    if not name or name != name.strip():
        raise ValueError("run spec requires a canonical runnable name")
    index = getattr(state, "runnables", None)
    if index is None:
        return "agent", resolve_runnable(state_program(state), name, kind=kind)
    entry = index.get(name)
    if entry is None or (kind is not None and entry.kind != kind):
        raise ToolangError(f"Runnable not found: {name}")
    return state.runnable_modules[name], entry


def resolve_state_runnable_query(
    state: AgentState,
    query: str,
) -> tuple[str, Runnable]:
    """Resolve one singular runnable collection query."""

    resolved = resolve_public_runnable_query(state, query)
    return resolved.module, resolved.executable


def resolve_public_runnable_query(
    state: AgentState,
    query: str,
) -> ResolvedRunnable:
    """Resolve one singular query with its effective public identity."""

    item = runnable_dataset(state).require_one(query, label="runnable")
    return ResolvedRunnable(
        name=item.name,
        module=item.module,
        executable=cast(Runnable, item.record),
    )


def resolve_module_runnable(
    state: AgentState,
    module_name: str,
    name: str,
    *,
    kind: str | None = None,
) -> tuple[str, Runnable]:
    """Resolve a module-local runnable and its effective public name."""

    resolve_indexed = getattr(state, "module_runnable", None)
    if not callable(resolve_indexed):
        runnable = resolve_runnable(state_program(state, module_name), name, kind=kind)
        return runnable.name, runnable
    entry = resolve_indexed(module_name, name, kind=kind)
    if entry is None:
        raise ToolangError(f"Runnable not found: {name}")
    public_name = next(
        (
            candidate_name
            for candidate_name, candidate in state.runnables.items()
            if state.runnable_modules[candidate_name] == module_name
            and candidate is entry
        ),
        entry.name,
    )
    return public_name, entry


def resolve_bound_runnable(
    state: AgentState,
    module_name: str,
    ref: str,
) -> Runnable:
    """Resolve a stored effective ref back to its Program declaration."""

    name, kind = parse_runnable_ref(ref)
    index = getattr(state, "runnables", None)
    if index is None:
        return resolve_runnable(state_program(state, module_name), name, kind=kind)
    public = index.get(name)
    if (
        public is not None
        and state.runnable_modules[name] == module_name
        and (kind is None or public.kind == kind)
    ):
        return public
    _effective_name, runnable = resolve_module_runnable(
        state,
        module_name,
        name,
        kind=kind,
    )
    return runnable


def parse_runnable_ref(value: str) -> tuple[str, str | None]:
    """Split one optional kind-qualified runnable reference."""

    return parse_public_runnable_ref(value)


def runnable_binding_defaults(
    program: Program | AgentState,
    binding: str | None,
    *,
    fallback_agic: str,
) -> tuple[str | None, str | None]:
    """Project one runnable binding into exclusive agic and flow defaults."""

    if binding is None:
        if isinstance(program, AgentState):
            fallback = program.runnables.get(fallback_agic)
            agic = (
                fallback_agic
                if fallback is not None and fallback.kind == "agic"
                else "default"
            )
        else:
            agic = (
                fallback_agic
                if program.find_agic(fallback_agic) is not None
                else "default"
            )
        return agic, None
    if isinstance(program, AgentState):
        runnable = resolve_state_runnable_query(program, binding)[1]
    else:
        name, kind = parse_runnable_ref(binding)
        runnable = resolve_runnable(program, name, kind=kind)
    name = runnable.name
    return (name, None) if isinstance(runnable, AgicDecl) else (None, name)


def runnable_descriptions(
    state: AgentState, routes: AgicRoutes
) -> tuple[dict[str, object], ...]:
    """Return data-only descriptions selected by authored routing authority."""
    if not routes.hands and not routes.handoffs:
        return ()
    return tuple(_runnable_description(state, route) for route in routes.resolved)


def runnable_input_contract(
    state: AgentState,
    module: str,
    runnable: Runnable,
) -> dict[str, object]:
    """Return the model-facing input contract for one runnable declaration."""

    structs = {item.name: item for item in state.modules[module].structs}
    signature_types = (
        *((runnable.input.type_name or "Part[]",) if runnable.input else ()),
        *(parameter.type_name or "Part[]" for parameter in runnable.params),
    )
    return {
        "input": (
            {
                "optional": runnable.input.optional,
                "type": runnable.input.type_name or "Part[]",
            }
            if runnable.input is not None
            else None
        ),
        "parameters": [
            {
                "name": parameter.name,
                "optional": parameter.optional,
                "type": parameter.type_name or "Part[]",
            }
            for parameter in runnable.params
        ],
        "structs": _reachable_structs(signature_types, structs=structs),
    }


def _runnable_description(
    state: AgentState,
    route: RunnableRoute,
) -> dict[str, object]:
    target = route.runnable
    runnable = target.executable
    module = target.module
    structs = {item.name: item for item in state.modules[module].structs}
    signature_types = (
        *((runnable.input.type_name or "Part[]",) if runnable.input else ()),
        *(parameter.type_name or "Part[]" for parameter in runnable.params),
        runnable.output or ("Part[]" if isinstance(runnable, AgicDecl) else "Json"),
    )
    return {
        "actions": list(route.actions),
        "documentation": (runnable.doc or "")[:RUNNABLE_DOCUMENTATION_MAX_CHARS],
        "input": (
            {
                "optional": runnable.input.optional,
                "type": runnable.input.type_name or "Part[]",
            }
            if runnable.input is not None
            else None
        ),
        "output": runnable.output
        or ("Part[]" if isinstance(runnable, AgicDecl) else "Json"),
        "parameters": [
            {
                "name": parameter.name,
                "optional": parameter.optional,
                "type": parameter.type_name or "Part[]",
            }
            for parameter in runnable.params
        ],
        "ref": target.ref,
        "structs": _reachable_structs(signature_types, structs=structs),
    }


def _reachable_structs(
    types: tuple[str, ...],
    *,
    structs: dict[str, StructDecl],
) -> list[dict[str, object]]:
    seen: set[str] = set()
    result: list[dict[str, object]] = []

    def visit(type_name: str) -> None:
        name = type_name
        while name.endswith("[]"):
            name = name[:-2]
        if name in _BUILTIN_TYPES or name in seen:
            return
        struct = structs.get(name)
        if struct is None:
            return
        seen.add(name)
        result.append(
            {
                "documentation": (struct.doc or "")[:RUNNABLE_DOCUMENTATION_MAX_CHARS],
                "fields": [
                    {
                        "name": field.name,
                        "optional": field.optional,
                        "type": field.type_name,
                    }
                    for field in struct.fields
                ],
                "name": struct.name,
            }
        )
        for field in struct.fields:
            visit(field.type_name)

    for type_name in types:
        visit(type_name)
    return result
