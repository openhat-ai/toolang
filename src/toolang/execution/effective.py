"""Effective model, tool, and cap selection for one run."""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
from typing import TYPE_CHECKING, cast

from toolang.base.protocols.tool import AgentTool
from toolang.base.types.model import ModelTarget

from .. import caps as cap_store
from ..lang.ast import SourceSpan, Thunk, Directive
from ..state.live import LiveState
from ..state.prepared import PreparedEntry
from ..tools.registry import selected_tool_names, tool_ref_for_model_tool
from .model import resolve_model, select_model_selectors

if TYPE_CHECKING:
    from ..state.program import LiveProgram
    from ..up import UptimeContext
    from .binding import RunBinding

_LOGGER = logging.getLogger("toolang.run")
_THREAD_THUNK_NAMES = frozenset({"chat", "task", "chore"})


@dataclass(frozen=True, slots=True)
class EffectiveRunSets:
    """Effective runtime resources selected for one thunk invocation."""

    models_base: tuple[str, ...]
    tools_base: dict[str, AgentTool]
    psyches_base: tuple[PreparedEntry, ...]
    skills_base: tuple[PreparedEntry, ...]
    services_base: tuple[PreparedEntry, ...]
    model_selectors: tuple[str, ...]
    models: tuple[ModelTarget, ...]
    tools: dict[str, AgentTool]
    psyches: tuple[PreparedEntry, ...]
    skills: tuple[PreparedEntry, ...]
    services: tuple[PreparedEntry, ...]
    set_math: dict[str, object]


def select_origin_thunk(
    program: LiveProgram,
    *,
    origin: str,
    thunk_name: str | None = None,
) -> Thunk:
    """Return the effective thunk for one run origin."""

    if thunk_name is not None:
        return program.get_thunk(thunk_name)
    if origin in _THREAD_THUNK_NAMES:
        thunk = _find_named_thunk(program.thunks, origin)
        if thunk is not None:
            return thunk
        return _default_thread_thunk(origin)
    return program.get_thunk(None)


def effective_origin_model_selectors(
    context: UptimeContext,
    *,
    origin: str,
    thunk_name: str | None = None,
) -> tuple[str, ...]:
    """Return effective model selectors for one run origin before per-run selection."""

    thunk = select_origin_thunk(
        context.live.program,
        origin=origin,
        thunk_name=thunk_name,
    )
    return effective_model_selectors(
        context,
        thunk=thunk,
        models_base=activation_allowed_model_selectors(context),
    )


def effective_run_sets(
    context: UptimeContext,
    *,
    run: RunBinding,
    thunk: Thunk,
) -> EffectiveRunSets:
    models_base = activation_allowed_model_selectors(context)
    tools_base = dict(context.tools)
    psyches_base = cap_entries(run.live, kind="psyche")
    skills_base = cap_entries(run.live, kind="skill")
    services_base = cap_entries(run.live, kind="service")
    effective_tools, tool_math = select_tools_with_trace(
        tools_base,
        thunk.directives_for("tool"),
    )
    effective_models, model_math = model_set_math(
        context,
        thunk=thunk,
        models_base=models_base,
    )
    effective_psyches, psyche_math = select_entries_with_trace(
        psyches_base,
        thunk.directives_for("psyche"),
    )
    effective_skills, skill_math = select_entries_with_trace(
        skills_base,
        thunk.directives_for("skill"),
    )
    effective_services, service_math = select_entries_with_trace(
        services_base,
        thunk.directives_for("service"),
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


def activation_default_model_selector(context: UptimeContext) -> str | None:
    value = context.config.get("models.default_selector")
    if not isinstance(value, str):
        return None
    selector = value.strip()
    return selector or None


def activation_allowed_model_selectors(context: UptimeContext) -> tuple[str, ...]:
    value = context.config.get("models.allowed_selectors")
    if isinstance(value, tuple):
        return tuple(item for item in value if isinstance(item, str) and item.strip())
    if isinstance(value, list):
        return tuple(item for item in value if isinstance(item, str) and item.strip())
    return ()


def effective_model_selectors(
    context: UptimeContext,
    *,
    thunk: Thunk,
    models_base: tuple[str, ...],
) -> tuple[str, ...]:
    effective, _math = model_set_math(
        context,
        thunk=thunk,
        models_base=models_base,
    )
    return effective


def model_set_math(
    context: UptimeContext,
    *,
    thunk: Thunk,
    models_base: tuple[str, ...],
) -> tuple[tuple[str, ...], dict[str, object]]:
    thunk_selectors, thunk_steps = _apply_string_directives_with_trace(
        (),
        thunk.directives_for("model"),
    )
    effective = select_model_selectors(
        context,
        thunk_selectors=thunk_selectors,
        activation_selectors=models_base,
        default_selector=activation_default_model_selector(context),
    )
    return (
        effective,
        {
            "activation_ceiling": list(models_base),
            "activation_default": activation_default_model_selector(context),
            "requested": None,
            "thunk_directive_base": [],
            "thunk_directive_steps": thunk_steps,
            "thunk_selectors": list(thunk_selectors),
            "effective": list(effective),
        },
    )


def thunk_model_refs(thunk: Thunk) -> tuple[str, ...]:
    return _apply_string_directives((), thunk.directives_for("model"))


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
    selected = {
        name: tools_base[name]
        for name in names
        if name in tools_base
    }
    return (
        selected,
        {
            "activation_ceiling": list(tools_base),
            "directive_steps": steps,
            "effective": list(selected),
        },
    )


def select_entries(
    base: tuple[PreparedEntry, ...],
    directives: tuple[Directive, ...],
) -> tuple[PreparedEntry, ...]:
    selected, _math = select_entries_with_trace(base, directives)
    return selected


def select_entries_with_trace(
    base: tuple[PreparedEntry, ...],
    directives: tuple[Directive, ...],
) -> tuple[tuple[PreparedEntry, ...], dict[str, object]]:
    entries, steps = _apply_cap_directives_with_trace(base, directives)
    return (
        entries,
        {
            "activation_ceiling": [_entry_label(entry) for entry in base],
            "directive_steps": steps,
            "effective": [_entry_label(entry) for entry in entries],
        },
    )


def cap_entries(live: LiveState, *, kind: str) -> tuple[PreparedEntry, ...]:
    return tuple(entry for entry in live.cap_entries if entry.kind == kind)


def resolve_runtime_models(
    context: UptimeContext,
    selectors: tuple[str, ...],
) -> tuple[ModelTarget, ...]:
    return tuple(
        resolve_model(
            context,
            selector=selector,
        )
        for selector in selectors
    )


def log_set_math(*, run: RunBinding, thunk: Thunk, set_math: dict[str, object]) -> None:
    if not _LOGGER.isEnabledFor(logging.DEBUG):
        return
    _LOGGER.debug(
        "run.activation thread=%s run=%s thunk=%s summary=%s math=%s",
        run.thread_id,
        run.run_id,
        thunk.thunk_name(),
        _set_math_summary(set_math),
        json.dumps(set_math, ensure_ascii=False, sort_keys=True),
    )


def _find_named_thunk(thunks: tuple[Thunk, ...], name: str) -> Thunk | None:
    for thunk in thunks:
        if thunk.thunk_name() == name:
            return thunk
    return None


def _default_thread_thunk(origin: str) -> Thunk:
    return Thunk(
        name=origin,
        span=SourceSpan(0),
    )


def _apply_tool_directives_with_trace(
    tools_base: dict[str, AgentTool],
    directives: tuple[Directive, ...],
) -> tuple[tuple[str, ...], list[dict[str, object]]]:
    current = list(tools_base)
    refs_by_model_name = {
        name: tool_ref_for_model_tool(name, tool)
        for name, tool in tools_base.items()
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
    base: tuple[PreparedEntry, ...],
    directives: tuple[Directive, ...],
) -> tuple[tuple[PreparedEntry, ...], list[dict[str, object]]]:
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
            current = [entry for entry in current if _entry_identity(entry) not in blocked]
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


def _entry_label(entry: PreparedEntry) -> str:
    return f"{entry.kind}/{entry.name}"


def _entry_identity(entry: PreparedEntry) -> tuple[str, str, str]:
    return (entry.kind, entry.name, entry.ref)


def _entry_agent_name(entries: tuple[PreparedEntry, ...]) -> str:
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
            parts.append(_domain_set_math_summary(domain, cast(dict[str, object], value)))
    return "; ".join(parts)


def _domain_set_math_summary(domain: str, value: dict[str, object]) -> str:
    base = value.get("activation_ceiling")
    effective = value.get("effective")
    base_count = len(base) if isinstance(base, list) else 0
    effective_count = len(effective) if isinstance(effective, list) else 0
    steps = value.get("thunk_directive_steps")
    if not isinstance(steps, list):
        steps = value.get("directive_steps")
    expression = _set_math_expression(cast(list[object], steps) if isinstance(steps, list) else [])
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
