"""Effective model, tool, and cap selection for one run."""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
from typing import TYPE_CHECKING, cast

from toolang.base.protocols.tool import AgentTool
from toolang.common.error import ToolangError
from toolang.base.types.model import ModelTarget

from toolang.state import state as cap_store
from ..lang.ast import AgicDecl, Directive, Parameter, Program, Span
from ..state.state import AgentState
from toolang.state.state import PreparedCap
from toolang.plugin.tools.registry import selected_tool_names, tool_ref_for_model_tool
from toolang.plugin.models.resolution import resolve_model, select_model_selectors

if TYPE_CHECKING:
    from .assembly import SupportsRunAssembly
    from .binding import _Run

_LOGGER = logging.getLogger("toolang.run")
_THREAD_AGIC_NAMES = frozenset({"chat", "task", "chore", "file"})
_RUNTIME_DEFAULT_AGIC = AgicDecl(
    name="default",
    input=Parameter(name="_", type_name="Part[]", span=Span(line=1)),
    span=Span(line=1),
)


@dataclass(frozen=True, slots=True)
class EffectiveRunSets:
    """Effective runtime resources selected for one agic invocation."""

    models_base: tuple[str, ...]
    tools_base: dict[str, AgentTool]
    psyches_base: tuple[PreparedCap, ...]
    skills_base: tuple[PreparedCap, ...]
    services_base: tuple[PreparedCap, ...]
    model_selectors: tuple[str, ...]
    models: tuple[ModelTarget, ...]
    tools: dict[str, AgentTool]
    psyches: tuple[PreparedCap, ...]
    skills: tuple[PreparedCap, ...]
    services: tuple[PreparedCap, ...]
    set_math: dict[str, object]


def select_origin_agic(
    program: Program,
    *,
    origin: str,
    agic_name: str | None = None,
) -> AgicDecl:
    """Return the effective agic for one run origin."""

    if agic_name is not None:
        return require_agic(program, agic_name)
    if origin in _THREAD_AGIC_NAMES:
        agic = program.find_agic(origin)
        if agic is not None:
            return agic
    return require_agic(program, "default")


def effective_agics(program: Program) -> tuple[AgicDecl, ...]:
    """Return authored agics with the runtime default when needed."""

    if program.find_agic("default") is not None:
        return program.agics
    return (*program.agics, _RUNTIME_DEFAULT_AGIC)


def require_agic(program: Program, name: str) -> AgicDecl:
    """Return one effective agic or raise when it does not exist."""

    agic = program.find_agic(name)
    if agic is None and name == "default":
        agic = _RUNTIME_DEFAULT_AGIC
    if agic is None:
        raise ToolangError(f"Agic not found: {name}")
    return agic


def effective_origin_model_selectors(
    context: SupportsRunAssembly,
    *,
    state: AgentState,
    origin: str,
    agic_name: str | None = None,
) -> tuple[str, ...]:
    """Return effective model selectors for one run origin before per-run selection."""

    agic = select_origin_agic(
        state.program,
        origin=origin,
        agic_name=agic_name,
    )
    return effective_model_selectors(
        context,
        agic=agic,
        models_base=activation_allowed_model_selectors(context),
    )


def effective_run_sets(
    context: SupportsRunAssembly,
    *,
    run: _Run,
    agic: AgicDecl,
) -> EffectiveRunSets:
    models_base = run_allowed_model_selectors(context, run=run)
    tools_base = run_tools_base(context, run=run)
    selected_cap_entries = run_cap_entries(context, run=run)
    psyches_base = cap_entries(selected_cap_entries, kind="psyche")
    skills_base = cap_entries(selected_cap_entries, kind="skill")
    services_base = cap_entries(selected_cap_entries, kind="service")
    effective_tools, tool_math = select_tools_with_trace(
        tools_base,
        directives_for(agic, "tools"),
    )
    effective_models, model_math = model_set_math(
        context,
        agic=agic,
        models_base=models_base,
    )
    effective_psyches, psyche_math = select_entries_with_trace(
        psyches_base,
        directives_for(agic, "psyches"),
    )
    effective_skills, skill_math = select_entries_with_trace(
        skills_base,
        directives_for(agic, "skills"),
    )
    effective_services, service_math = select_entries_with_trace(
        services_base,
        directives_for(agic, "services"),
    )
    set_math: dict[str, object] = {
        "models": model_math,
        "tools": tool_math,
        "psyches": psyche_math,
        "skills": skill_math,
        "services": service_math,
    }
    return EffectiveRunSets(
        models_base=models_base,
        tools_base=tools_base,
        psyches_base=psyches_base,
        skills_base=skills_base,
        services_base=services_base,
        model_selectors=effective_models,
        models=resolve_runtime_models(context, effective_models),
        tools=effective_tools,
        psyches=effective_psyches,
        skills=effective_skills,
        services=effective_services,
        set_math=set_math,
    )


def activation_default_model_selector(context: SupportsRunAssembly) -> str | None:
    value = context.config.get("models.default_selector")
    if not isinstance(value, str):
        return None
    selector = value.strip()
    return selector or None


def activation_allowed_model_selectors(
    context: SupportsRunAssembly,
) -> tuple[str, ...]:
    value = context.config.get("models.allowed_selectors")
    if isinstance(value, tuple):
        return tuple(item for item in value if isinstance(item, str) and item.strip())
    if isinstance(value, list):
        return tuple(item for item in value if isinstance(item, str) and item.strip())
    return ()


def run_allowed_model_selectors(
    context: SupportsRunAssembly, *, run: _Run
) -> tuple[str, ...]:
    activation_selectors = activation_allowed_model_selectors(context)
    if not run.model_selectors:
        return activation_selectors
    if activation_selectors:
        return select_model_selectors(
            context,
            agic_selectors=run.model_selectors,
            activation_selectors=activation_selectors,
            default_selector=activation_default_model_selector(context),
        )
    return select_model_selectors(
        context,
        activation_selectors=run.model_selectors,
        default_selector=activation_default_model_selector(context),
    )


def run_tools_base(context: SupportsRunAssembly, *, run: _Run) -> dict[str, AgentTool]:
    tools = dict(run.setup.tools)
    selectors = run.tool_selectors
    if selectors is None:
        return tools
    if not selectors:
        return {}
    refs_by_model_name = {
        name: tool_ref_for_model_tool(name, tool) for name, tool in tools.items()
    }
    missing = [
        selector
        for selector in selectors
        if not selected_tool_names(refs_by_model_name, (selector,))
    ]
    if missing:
        raise ToolangError(f"tool selector matched no tools: {', '.join(missing)}")
    selected_names = selected_tool_names(refs_by_model_name, selectors)
    return {name: tools[name] for name in selected_names if name in tools}


def run_cap_entries(
    context: SupportsRunAssembly, *, run: _Run
) -> tuple[PreparedCap, ...]:
    entries = tuple(run.state.caps)
    selectors = run.cap_selectors
    if not selectors:
        return entries
    missing = [
        selector
        for selector in selectors
        if not cap_store.select_cap_entries(
            entries,
            (selector,),
            agent_name=context.name,
        )
    ]
    if missing:
        raise ToolangError(f"cap selector matched no caps: {', '.join(missing)}")
    return cap_store.select_cap_entries(
        entries,
        selectors,
        agent_name=context.name,
    )


def effective_model_selectors(
    context: SupportsRunAssembly,
    *,
    agic: AgicDecl,
    models_base: tuple[str, ...],
) -> tuple[str, ...]:
    effective, _math = model_set_math(
        context,
        agic=agic,
        models_base=models_base,
    )
    return effective


def model_set_math(
    context: SupportsRunAssembly,
    *,
    agic: AgicDecl,
    models_base: tuple[str, ...],
) -> tuple[tuple[str, ...], dict[str, object]]:
    agic_selectors, agic_steps = _apply_string_directives_with_trace(
        (),
        directives_for(agic, "models"),
    )
    effective = select_model_selectors(
        context,
        agic_selectors=agic_selectors,
        activation_selectors=models_base,
        default_selector=activation_default_model_selector(context),
    )
    return (
        effective,
        {
            "activation_ceiling": list(models_base),
            "activation_default": activation_default_model_selector(context),
            "requested": None,
            "agic_directive_base": [],
            "agic_directive_steps": agic_steps,
            "agic_selectors": list(agic_selectors),
            "effective": list(effective),
        },
    )


def agic_model_refs(agic: AgicDecl) -> tuple[str, ...]:
    return _apply_string_directives((), directives_for(agic, "models"))


def select_tools(
    tools_base: dict[str, AgentTool],
    directives: tuple[Directive, ...],
) -> dict[str, AgentTool]:
    selected, _math = select_tools_with_trace(tools_base, directives)
    return selected


def select_tools_with_trace(
    tools_base: dict[str, AgentTool],
    directives: tuple[Directive, ...],
) -> tuple[dict[str, AgentTool], dict[str, object]]:
    names, steps = _apply_tool_directives_with_trace(tools_base, directives)
    selected = {name: tools_base[name] for name in names if name in tools_base}
    return (
        selected,
        {
            "activation_ceiling": list(tools_base),
            "directive_steps": steps,
            "effective": list(selected),
        },
    )


def select_entries(
    base: tuple[PreparedCap, ...],
    directives: tuple[Directive, ...],
) -> tuple[PreparedCap, ...]:
    selected, _math = select_entries_with_trace(base, directives)
    return selected


def select_entries_with_trace(
    base: tuple[PreparedCap, ...],
    directives: tuple[Directive, ...],
) -> tuple[tuple[PreparedCap, ...], dict[str, object]]:
    entries, steps = _apply_cap_directives_with_trace(base, directives)
    return (
        entries,
        {
            "activation_ceiling": [_entry_label(entry) for entry in base],
            "directive_steps": steps,
            "effective": [_entry_label(entry) for entry in entries],
        },
    )


def cap_entries(
    entries: AgentState | tuple[PreparedCap, ...], *, kind: str
) -> tuple[PreparedCap, ...]:
    cap_entries = entries.caps if isinstance(entries, AgentState) else entries
    return tuple(entry for entry in cap_entries if entry.kind == kind)


def resolve_runtime_models(
    context: SupportsRunAssembly,
    selectors: tuple[str, ...],
) -> tuple[ModelTarget, ...]:
    return tuple(
        resolve_model(
            context,
            selector=selector,
        )
        for selector in selectors
    )


def log_set_math(*, run: _Run, agic: AgicDecl, set_math: dict[str, object]) -> None:
    if not _LOGGER.isEnabledFor(logging.DEBUG):
        return
    _LOGGER.debug(
        "run.activation thread=%s run=%s agic=%s summary=%s math=%s",
        run.thread_id,
        run.run_id,
        agic.name,
        _set_math_summary(set_math),
        json.dumps(set_math, ensure_ascii=False, sort_keys=True),
    )


def directives_for(agic: AgicDecl, name: str) -> tuple[Directive, ...]:
    return tuple(item for item in agic.directives if item.name == name)


def _apply_tool_directives_with_trace(
    tools_base: dict[str, AgentTool],
    directives: tuple[Directive, ...],
) -> tuple[tuple[str, ...], list[dict[str, object]]]:
    current = list(tools_base)
    refs_by_model_name = {
        name: tool_ref_for_model_tool(name, tool) for name, tool in tools_base.items()
    }
    steps: list[dict[str, object]] = []
    for directive in directives:
        selectors = tuple(item for item in directive.values if item)
        before = tuple(current)
        matches = selected_tool_names(refs_by_model_name, selectors)
        op = _directive_operation(directive)
        if op == "set":
            current = list(matches)
        elif op == "add":
            for name in matches:
                if name not in current:
                    current.append(name)
        elif op == "remove":
            blocked = set(matches)
            current = [name for name in current if name not in blocked]
        steps.append(
            _directive_step(
                directive=directive,
                selectors=selectors,
                matches=matches,
                before=before,
                after=tuple(current),
            )
        )
    return tuple(current), steps


def _apply_cap_directives_with_trace(
    base: tuple[PreparedCap, ...],
    directives: tuple[Directive, ...],
) -> tuple[tuple[PreparedCap, ...], list[dict[str, object]]]:
    current = list(base)
    kind = base[0].kind if base else None
    agent_name = _entry_agent_name(base)
    steps: list[dict[str, object]] = []
    for directive in directives:
        selectors = tuple(item for item in directive.values if item)
        before = tuple(current)
        matches = cap_store.select_cap_entries(
            base,
            selectors,
            agent_name=agent_name,
            implicit_kind=kind,
        )
        op = _directive_operation(directive)
        if op == "set":
            current = list(matches)
        elif op == "add":
            seen = {_entry_identity(entry) for entry in current}
            for entry in matches:
                identity = _entry_identity(entry)
                if identity not in seen:
                    current.append(entry)
                    seen.add(identity)
        elif op == "remove":
            blocked = {_entry_identity(entry) for entry in matches}
            current = [
                entry for entry in current if _entry_identity(entry) not in blocked
            ]
        steps.append(
            _directive_step(
                directive=directive,
                selectors=selectors,
                matches=tuple(_entry_label(entry) for entry in matches),
                before=tuple(_entry_label(entry) for entry in before),
                after=tuple(_entry_label(entry) for entry in current),
            )
        )
    return tuple(current), steps


def _apply_string_directives_with_trace(
    base: tuple[str, ...],
    directives: tuple[Directive, ...],
) -> tuple[tuple[str, ...], list[dict[str, object]]]:
    current = list(dict.fromkeys(item for item in base if item))
    steps: list[dict[str, object]] = []
    for directive in directives:
        directive_items = tuple(item for item in directive.values if item)
        before = tuple(current)
        op = _directive_operation(directive)
        if op == "set":
            current = list(dict.fromkeys(directive_items))
        elif op == "add":
            for item in directive_items:
                if item not in current:
                    current.append(item)
        elif op == "remove":
            blocked = set(directive_items)
            current = [item for item in current if item not in blocked]
        steps.append(
            _directive_step(
                directive=directive,
                selectors=directive_items,
                matches=directive_items,
                before=before,
                after=tuple(current),
            )
        )
    return tuple(current), steps


def _apply_string_directives(
    base: tuple[str, ...],
    directives: tuple[Directive, ...],
) -> tuple[str, ...]:
    current = list(dict.fromkeys(item for item in base if item))
    for directive in directives:
        directive_items = [item for item in directive.values if item]
        op = _directive_operation(directive)
        if op == "set":
            current = list(dict.fromkeys(directive_items))
            continue
        if op == "add":
            for item in directive_items:
                if item not in current:
                    current.append(item)
            continue
        if op == "remove":
            blocked = set(directive_items)
            current = [item for item in current if item not in blocked]
    return tuple(current)


def _directive_step(
    *,
    directive: Directive,
    selectors: tuple[str, ...],
    matches: tuple[str, ...],
    before: tuple[str, ...],
    after: tuple[str, ...],
) -> dict[str, object]:
    return {
        "op": directive.operator,
        "line": directive.span.line,
        "selectors": list(selectors),
        "matches": list(matches),
        "before": list(before),
        "after": list(after),
    }


def _entry_label(entry: PreparedCap) -> str:
    return f"{entry.kind}/{entry.name}"


def _entry_identity(entry: PreparedCap) -> tuple[str, str, str]:
    return (entry.kind, entry.name, entry.ref)


def _entry_agent_name(entries: tuple[PreparedCap, ...]) -> str:
    for entry in entries:
        path = entry.path or entry.source.path
        prefix, separator, rest = path.partition("agents/")
        del prefix
        if separator and "/" in rest:
            return rest.split("/", 1)[0]
    return "default"


def _set_math_summary(set_math: dict[str, object]) -> str:
    parts: list[str] = []
    for domain in ("models", "tools", "psyches", "skills", "services"):
        value = set_math.get(domain)
        if isinstance(value, dict):
            parts.append(
                _domain_set_math_summary(domain, cast(dict[str, object], value))
            )
    return "; ".join(parts)


def _domain_set_math_summary(domain: str, value: dict[str, object]) -> str:
    base = value.get("activation_ceiling")
    effective = value.get("effective")
    base_count = len(base) if isinstance(base, list) else 0
    effective_count = len(effective) if isinstance(effective, list) else 0
    steps = value.get("agic_directive_steps")
    if not isinstance(steps, list):
        steps = value.get("directive_steps")
    expression = _set_math_expression(
        cast(list[object], steps) if isinstance(steps, list) else []
    )
    return f"{domain} {base_count} {expression} -> {effective_count}"


def _set_math_expression(steps: list[object]) -> str:
    if not steps:
        return "activation"
    expressions: list[str] = []
    for step in steps:
        if not isinstance(step, dict):
            continue
        step_data = cast(dict[str, object], step)
        op = _directive_op_symbol(step_data.get("op"))
        selectors = step_data.get("selectors")
        selector_text = (
            ",".join(str(item) for item in selectors)
            if isinstance(selectors, list) and selectors
            else "-"
        )
        expressions.append(f"{op} {selector_text}")
    return " ; ".join(expressions) if expressions else "activation"


def _directive_op_symbol(value: object) -> str:
    return str(value)


def _directive_operation(directive: Directive) -> str:
    if directive.operator == "=":
        return "set"
    if directive.operator == "+=":
        return "add"
    if directive.operator == "-=":
        return "remove"
    return directive.operator
