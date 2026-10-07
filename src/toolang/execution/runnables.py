"""Runnable declaration lookup shared by binding and execution."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, TypeAlias

from toolang.base.errors import ToolangError
from toolang.lang.ast import (
    AgicDecl,
    FlowDecl,
    Program,
    StructDecl,
)
from toolang.lang.types import parse_runnable_ref_parts, unnamed_runnable_name
from toolang.state.state import (
    AgentState,
    program_runnable_index,
    state_program,
)

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
        "ReasoningPart",
        "ImagePart",
        "AudioPart",
        "DocumentPart",
        "ToolCallPart",
        "ToolResultPart",
    }
)


@dataclass(frozen=True, slots=True)
class ResolvedRunnable:
    """One runnable resolved to its owning State module."""

    name: str
    module: str
    executable: Runnable

    @property
    def identity(self) -> str:
        """Return a module/name identity independent of the selected revision."""
        return f"{self.module}::{self.name}"

    @property
    def ref(self) -> str:
        """Return the kind-qualified runnable reference."""

        return f"{self.executable.kind}:{self.name}"

    @property
    def qualified(self) -> str:
        """Return the stable State-local runnable identity."""

        return f"{self.module}::{self.ref}"


RouteAction: TypeAlias = Literal["run", "spawn", "exec"]


@dataclass(frozen=True, slots=True)
class RunnableRoute:
    """One currently resolved target and its allowed model actions."""

    runnable: ResolvedRunnable
    actions: tuple[RouteAction, ...]


@dataclass(frozen=True, slots=True)
class AgicRoutes:
    """Authored Agic routing authority and State-resolved model hints."""

    hands: tuple[str, ...] = ()
    handoffs: tuple[str, ...] = ()
    resolved: tuple[RunnableRoute, ...] = ()

    def scopes(self, state: AgentState) -> tuple[str, str, str]:
        """Project effective restrictions independently of active-path eligibility."""

        def scope(action: RouteAction, references: tuple[str, ...]) -> str:
            if not references or references == ("*",):
                return "ALL"
            refs = (
                runnable_ref(state, route.runnable)
                for route in self.resolved
                if action in route.actions
            )
            return ",".join(refs) or "NONE"

        hands = scope("run", self.hands)
        return hands, scope("exec", self.handoffs), hands

    def allows(self, action: RouteAction, target: ResolvedRunnable) -> bool:
        """Return whether authored routing authority permits one target."""

        return any(
            route.runnable.qualified == target.qualified and action in route.actions
            for route in self.resolved
        )


def resolve_public_runnable(
    state: AgentState,
    name: str,
    *,
    kind: str | None = None,
) -> ResolvedRunnable:
    """Resolve one public runnable with its effective name and owner module."""

    parsed = parse_runnable_ref_parts(name)
    if kind is not None and parsed.kind is not None and kind != parsed.kind:
        raise ToolangError(f"Runnable kind does not match: {name}")
    module, executable = resolve_state_runnable(
        state, parsed.name, kind=kind or parsed.kind
    )
    if parsed.module is not None and parsed.module != module:
        raise ToolangError(f"Runnable not found: {name}")
    return ResolvedRunnable(name=parsed.name, module=module, executable=executable)


def resolve_agic_routes(
    state: AgentState,
    agic: Runnable,
    *,
    hands: tuple[str, ...] | None = None,
    handoffs: tuple[str, ...] | None = None,
    module: str = "agent",
) -> AgicRoutes:
    """Resolve one Agic's routes within its module's visible namespace."""

    hands = _directive_values(agic, "hands") if hands is None else hands
    handoffs = _directive_values(agic, "handoffs") if handoffs is None else handoffs
    actions_by_ref: dict[str, set[RouteAction]] = {}
    groups: tuple[tuple[RouteAction, tuple[str, ...]], ...] = (
        ("run", hands),
        ("spawn", hands),
        ("exec", handoffs),
    )
    targets = visible_runnables(state, module)
    for route_action, references in groups:
        if references == ("none",):
            continue
        if not references or references == ("*",):
            selected = targets
        else:
            refs = tuple(parse_runnable_ref_parts(value) for value in references)
            selected = tuple(
                item
                for item in targets
                if any(
                    item.name == ref.name
                    and (ref.kind is None or item.executable.kind == ref.kind)
                    and (ref.module is None or item.module == ref.module)
                    for ref in refs
                )
            )
        for item in selected:
            target = ResolvedRunnable(
                name=item.name,
                module=item.module,
                executable=item.executable,
            )
            actions_by_ref.setdefault(target.ref, set()).add(route_action)
    resolved = tuple(
        RunnableRoute(
            runnable=ResolvedRunnable(
                name=item.name,
                module=item.module,
                executable=item.executable,
            ),
            actions=tuple(
                action for action in ("run", "spawn", "exec") if action in actions
            ),
        )
        for item in targets
        if (actions := actions_by_ref.get(item.ref)) is not None
    )
    return AgicRoutes(hands=hands, handoffs=handoffs, resolved=resolved)


def visible_runnables(state: AgentState, module: str) -> tuple[ResolvedRunnable, ...]:
    """Enumerate the caller's namespace without route or active-path filtering."""

    if module == "agent":
        index = getattr(state, "runnables", None)
        if index is None:
            index = program_runnable_index(state_program(state))
        return tuple(resolve_public_runnable(state, name) for name in index)
    if module not in state.modules:
        return ()
    return tuple(
        resolve_call_target(state, module, name)
        for name in program_runnable_index(state_program(state, module))
    )


def runnable_ref(state: AgentState, target: ResolvedRunnable) -> str:
    """Use public aliases where unique; qualify private module identities."""

    public = state.runnables.get(target.name)
    if (
        public is not None
        and state.runnable_modules[target.name] == target.module
        and public.kind == target.executable.kind
    ):
        return target.ref
    return target.qualified


def bound_runnable(state: AgentState, module: str, reference: str) -> ResolvedRunnable:
    """Resolve a captured binding without adopting a newer declaration."""

    parsed = parse_runnable_ref_parts(reference)
    executable = resolve_bound_runnable(state, module, reference)
    return ResolvedRunnable(parsed.name, module, executable)


def _directive_values(agic: Runnable, name: str) -> tuple[str, ...]:
    directive = next((item for item in agic.directives if item.name == name), None)
    return directive.values if directive is not None else ()


def unnamed_ref_name(runnable: Runnable, *, role: str) -> str:
    """Return the stored ref name for one unnamed or named declaration."""

    if runnable.name is not None:
        return runnable.name
    return unnamed_runnable_name(role, runnable.span.line)


def resolve_runnable(
    program: Program,
    name: str,
    *,
    kind: str | None = None,
) -> Runnable:
    """Resolve one unique runnable and optionally require its declaration kind."""

    if not name or name != name.strip():
        raise ValueError("run spec requires a canonical runnable name")
    try:
        index = program_runnable_index(program)
    except ValueError as exc:
        raise ToolangError(str(exc)) from exc
    entry = _lookup_index(index, name, kind=kind)
    if entry is None:
        raise ToolangError(f"Runnable not found: {name}")
    return entry


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
    key, entry = _lookup_index_item(index, name, kind=kind)
    if entry is None or key is None:
        raise ToolangError(f"Runnable not found: {name}")
    return state.runnable_modules[key], entry


def resolve_runnable_reference(
    state: AgentState | Program,
    reference: str,
) -> ResolvedRunnable:
    """Resolve an exact name, kind, module, or line-qualified reference."""
    parsed = parse_runnable_ref_parts(reference)
    index = (
        program_runnable_index(state)
        if isinstance(state, Program)
        else getattr(state, "runnables", None)
    )
    if index is None:
        index = program_runnable_index(state_program(state))
    modules = (
        {name: "agent" for name in index}
        if isinstance(state, Program) or not hasattr(state, "runnable_modules")
        else state.runnable_modules
    )
    if parsed.module is not None:
        index = {
            name: item for name, item in index.items() if modules[name] == parsed.module
        }
    if parsed.name == "_":
        entries = [
            item
            for name, item in index.items()
            if name.startswith("<entry:")
            and (parsed.kind is None or item.kind == parsed.kind)
        ]
        if len(entries) > 1:
            raise ToolangError(f"Runnable reference is ambiguous: {reference}")
    key, entry = _lookup_index_item(index, reference)
    if key is not None and entry is not None:
        return ResolvedRunnable(name=key, module=modules[key], executable=entry)
    raise ToolangError(f"Runnable not found: {reference}")


def resolve_module_runnable(
    state: AgentState,
    module_name: str,
    name: str,
    *,
    kind: str | None = None,
) -> tuple[str, Runnable]:
    """Resolve a module-local runnable and its effective public name."""

    resolve_indexed = getattr(state, "module_runnable", None)
    parsed = (
        parse_runnable_ref_parts(name) if name.startswith("<") or ":" in name else None
    )
    if parsed is not None and parsed.role == "adhoc":
        if parsed.line is None:
            raise ToolangError(f"Runnable not found: {name}")
        program = state_program(state, module_name)
        matches = [
            item
            for item in program.agics
            if item.name is None
            and item.span.line == parsed.line
            and (kind in {None, "agic"})
            and (parsed.kind in {None, "agic"})
        ]
        if len(matches) != 1:
            raise ToolangError(f"Runnable not found: {name}")
        return unnamed_ref_name(matches[0], role="adhoc"), matches[0]
    if not callable(resolve_indexed):
        runnable = resolve_runnable(state_program(state, module_name), name, kind=kind)
        return name, runnable
    lookup_name = parsed.name if parsed is not None else name
    entry = resolve_indexed(
        module_name, lookup_name, kind=kind or (parsed.kind if parsed else None)
    )
    if (
        entry is None
        and parsed is not None
        and parsed.role == "entry"
        and parsed.line is None
    ):
        program = state_program(state, module_name)
        entry = resolve_runnable(program, name, kind=kind or parsed.kind)
    if entry is None:
        raise ToolangError(f"Runnable not found: {name}")
    public_name = next(
        (
            candidate_name
            for candidate_name, candidate in state.runnables.items()
            if state.runnable_modules[candidate_name] == module_name
            and candidate is entry
        ),
        name,
    )
    return public_name, entry


def resolve_bound_runnable(
    state: AgentState,
    module_name: str,
    ref: str,
) -> Runnable:
    """Resolve a stored effective ref back to its Program declaration."""

    parsed = parse_runnable_ref_parts(ref)
    name, kind = parsed.name, parsed.kind
    if parsed.module is not None:
        module_name = parsed.module
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


def resolve_call_target(
    state: AgentState, module: str, reference: str
) -> ResolvedRunnable:
    """Resolve an authored call without crossing a flow module's boundary."""

    parsed = parse_runnable_ref_parts(reference)
    if parsed.module is None or parsed.module == module:
        try:
            name, runnable = resolve_module_runnable(
                state, module, parsed.name, kind=parsed.kind
            )
            return ResolvedRunnable(name, module, runnable)
        except ToolangError as exc:
            if not str(exc).startswith("Runnable not found:"):
                raise
    target = resolve_public_runnable(state, reference)
    if target.module == module or (
        module == "agent" and isinstance(target.executable, FlowDecl)
    ):
        return target
    raise ToolangError(f"Runnable not found in module {module}: {reference}")


def parse_runnable_ref(value: str) -> tuple[str, str | None]:
    """Split one optional kind-qualified runnable reference."""

    parsed = parse_runnable_ref_parts(value)
    return parsed.name, parsed.kind


def _lookup_index(
    index: dict[str, Runnable],
    name: str,
    *,
    kind: str | None = None,
) -> Runnable | None:
    _key, entry = _lookup_index_item(index, name, kind=kind)
    return entry


def _lookup_index_item(
    index: dict[str, Runnable],
    name: str,
    *,
    kind: str | None = None,
) -> tuple[str | None, Runnable | None]:
    parsed = None
    try:
        parsed = parse_runnable_ref_parts(name)
        name = parsed.name
        if kind is None:
            kind = parsed.kind
    except ValueError:
        parsed = None
    if parsed is not None and parsed.role == "entry" and parsed.line is None:
        if parsed.name != "_":
            return None, None
        matches = [
            (key, item)
            for key, item in index.items()
            if key.startswith("<entry:") and (kind is None or item.kind == kind)
        ]
        if len(matches) != 1:
            return None, None
        return matches[0]
    entry = index.get(name)
    if entry is None or (kind is not None and entry.kind != kind):
        return None, None
    if (
        parsed is not None
        and parsed.line is not None
        and entry.span.line != parsed.line
    ):
        return None, None
    return name, entry


def runnable_fallback(program: Program | AgentState, *, preferred: str) -> str:
    """Choose a surface entry, then the runtime fallback."""

    index = (
        program.runnables
        if isinstance(program, AgentState)
        else program_runnable_index(program)
    )
    if preferred in index:
        return preferred
    matches = [name for name in index if name.startswith("<entry:")]
    if len(matches) == 1:
        return matches[0]
    raise ToolangError(
        f"agent has no {preferred} entry or unnamed entry; add one to agent.too"
    )


def runnable_binding_defaults(
    program: Program | AgentState,
    binding: str | None,
    *,
    fallback_agic: str,
) -> tuple[str | None, str | None]:
    """Project one runnable binding into exclusive agic and flow defaults."""

    if binding is None:
        binding = runnable_fallback(program, preferred=fallback_agic)
    resolved = resolve_runnable_reference(program, binding)
    name, runnable = resolved.name, resolved.executable
    return (name, None) if isinstance(runnable, AgicDecl) else (None, name)


def available_runnable_defaults(
    program: Program | AgentState,
    *,
    fallback_agic: str,
) -> tuple[str | None, str | None]:
    """Project a default runnable, or empty when the agent declares none."""

    try:
        return runnable_binding_defaults(
            program,
            None,
            fallback_agic=fallback_agic,
        )
    except ToolangError:
        return None, None


def runnable_signature(
    state: AgentState,
    module: str,
    runnable: Runnable,
    *,
    documentation_limit: int | None = RUNNABLE_DOCUMENTATION_MAX_CHARS,
) -> dict[str, object]:
    """Describe one signature for declarations and input-validation feedback."""

    structs = {item.name: item for item in state.modules[module].structs}
    output = runnable.output or ("Part[]" if isinstance(runnable, AgicDecl) else "Json")
    signature_types = (
        *((runnable.input.type_name or "Part[]",) if runnable.input else ()),
        *(parameter.type_name or "Part[]" for parameter in runnable.params),
        output,
    )
    return {
        "input": (
            {
                "documentation": (runnable.input.doc or "")[:documentation_limit],
                "optional": runnable.input.optional,
                "type": runnable.input.type_name or "Part[]",
            }
            if runnable.input is not None
            else None
        ),
        "parameters": [
            {
                "documentation": (parameter.doc or "")[:documentation_limit],
                "name": parameter.name,
                "optional": parameter.optional,
                "type": parameter.type_name or "Part[]",
            }
            for parameter in runnable.params
        ],
        "output": output,
        "structs": _reachable_structs(
            signature_types, structs=structs, documentation_limit=documentation_limit
        ),
    }


def _reachable_structs(
    types: tuple[str, ...],
    *,
    structs: dict[str, StructDecl],
    documentation_limit: int | None = RUNNABLE_DOCUMENTATION_MAX_CHARS,
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
                "documentation": (struct.doc or "")[:documentation_limit],
                "fields": [
                    {
                        "name": field.name,
                        "optional": field.optional,
                        "type": field.type_name,
                        **(
                            {"documentation": field.doc or ""}
                            if documentation_limit is None
                            else {}
                        ),
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
