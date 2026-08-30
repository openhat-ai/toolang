"""Parse and resolve caller commands for one execution policy."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from decimal import Decimal, InvalidOperation
import shlex

from toolang.base.errors import ToolangError
from toolang.base.types.policy import AgentCeiling, RunBindings, RunLimits
from toolang.common.selectors import parse_selector, split_selector_list
from toolang.lang.input import NamedInputSource, NamedInputSources
from toolang.setup import AgentSetup

from .types import (
    ALLOW_POLICY_FIELDS,
    DEFAULT_POLICY_FIELDS,
    LIMIT_POLICY_FIELDS,
    RunOverride,
)

_ALLOW_SHORTCUTS = ALLOW_POLICY_FIELDS
_DEFAULT_SHORTCUTS = frozenset({"model", "agic", "flow", "runnable"})
_CAP_KIND_BY_FIELD = {
    "psyches": "psyche",
    "skills": "skill",
    "services": "service",
    "prompts": "prompt",
}


def parse_run_override(
    line: str,
) -> tuple[RunOverride, NamedInputSources]:
    """Parse one canonical policy command or supported shortcut."""

    try:
        parsed = _try_parse_command(line)
    except ToolangError as error:
        raise ValueError(str(error)) from error
    if parsed is None:
        raise ValueError("line is not a policy command")
    return parsed


def parse_policy_prefix(
    source: str,
) -> tuple[tuple[RunOverride, ...], NamedInputSources, str]:
    """Parse the leading policy section and return its remaining source."""

    lines = _lines(source)
    commands: list[RunOverride] = []
    named: list[NamedInputSource] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if _is_blank(line.text):
            if not commands:
                break
            index += 1
            continue
        try:
            parsed = _try_parse_command(line.text)
        except ToolangError as error:
            raise ValueError(str(error)) from error
        if parsed is None:
            break
        command, command_named = parsed
        commands.append(command)
        named.extend(command_named)
        index += 1

    remaining = source[lines[index].start :] if index < len(lines) else ""
    return _normalize_commands(commands), tuple(named), remaining


def merge_commands(
    current: Sequence[RunOverride],
    updates: Sequence[RunOverride],
) -> tuple[RunOverride, ...]:
    """Apply one policy-only edit to a compact session command sequence."""

    merged = list(_normalize_commands(current))
    for command in _normalize_commands(updates):
        key = (command.group, command.field)
        merged = [item for item in merged if (item.group, item.field) != key]
        if command.value is None and command.group in {"allow", "default"}:
            continue
        merged.append(command)
    return tuple(merged)


def resolve_commands(
    setup: AgentSetup,
    *,
    surface: RunBindings = RunBindings(),
    session: Sequence[RunOverride] = (),
    run: Sequence[RunOverride] = (),
) -> tuple[tuple[AgentCeiling, ...], RunBindings, RunLimits]:
    """Resolve policy layers against one current setup snapshot."""

    base = RunBindings(
        model=surface.model if surface.model is not None else setup.bindings.model,
        runnable=(
            surface.runnable
            if surface.runnable is not None
            else setup.bindings.runnable
        ),
    )
    return materialize_policy(
        base,
        setup.limits,
        session=session,
        run=run,
    )


def materialize_policy(
    defaults: RunBindings,
    default_limits: RunLimits,
    *,
    session: Sequence[RunOverride] = (),
    run: Sequence[RunOverride] = (),
) -> tuple[tuple[AgentCeiling, ...], RunBindings, RunLimits]:
    """Materialize session and input-local policy over concrete defaults."""

    bindings = _apply_binding_commands(defaults, defaults, session)
    bindings = _apply_binding_commands(bindings, defaults, run)
    limits = _apply_limit_commands(default_limits, session)
    limits = _apply_limit_commands(limits, run)
    ceilings = tuple(
        ceiling
        for commands in (session, run)
        if (ceiling := _command_agent_ceiling(commands)) is not None
    )
    return ceilings, bindings, limits


def _try_parse_command(
    line: str,
) -> tuple[RunOverride, NamedInputSources] | None:
    if not line.startswith(":") or line.startswith("::"):
        return None
    try:
        tokens = shlex.split(line, comments=False, posix=True)
    except ValueError as error:
        raise ValueError(str(error)) from error
    if not tokens:
        return None
    name = tokens[0][1:]
    if name in {"allow", "default", "limit"}:
        if len(tokens) != 2:
            raise ValueError(f":{name} expects one field=value assignment")
        field, raw = _assignment(tokens[1], command=f":{name}")
        return _canonical_command(name, field, (raw,)), ()
    if name in _DEFAULT_SHORTCUTS:
        if len(tokens) == 1:
            return None
        return _default_shortcut(name, tokens[1:])
    if name in _ALLOW_SHORTCUTS:
        if len(tokens) == 1:
            if name == "models":
                return None
            raise ValueError(f":{name} requires selectors, all, or none")
        return _canonical_command("allow", name, tuple(tokens[1:])), ()
    return None


def _canonical_command(
    group: str,
    field: str,
    raw_values: tuple[str, ...],
) -> RunOverride:
    if group == "allow":
        if field not in ALLOW_POLICY_FIELDS:
            raise ValueError(f"unknown allow field: {field}")
        value = _allow_value(field, raw_values)
    elif group == "default":
        if field not in DEFAULT_POLICY_FIELDS:
            raise ValueError(f"unknown default field: {field}")
        value = _default_value(field, raw_values[0])
    elif group == "limit":
        if field not in LIMIT_POLICY_FIELDS:
            raise ValueError(f"unknown run limit: {field}")
        value = _limit_value(field, raw_values[0])
    else:  # pragma: no cover - callers use the closed parser grammar
        raise ValueError(f"unknown policy command group: {group}")
    if group == "allow":
        return RunOverride("allow", field, value)
    if group == "default":
        return RunOverride("default", field, value)
    return RunOverride("limit", field, value)


def _default_shortcut(
    name: str,
    values: list[str],
) -> tuple[RunOverride, NamedInputSources]:
    selector = values[0]
    if name == "model":
        if len(values) != 1:
            raise ValueError(":model accepts no named inputs")
        value = None if selector == "default" else _default_value("model", selector)
        return RunOverride("default", "model", value), ()

    if selector == "default":
        if len(values) != 1:
            raise ValueError(f":{name} default accepts no named inputs")
        return RunOverride("default", "runnable", None), ()
    runnable = f"{name}:{selector}" if name in {"agic", "flow"} else selector
    command = RunOverride(
        "default",
        "runnable",
        _default_value("runnable", runnable),
    )
    return command, _named_inputs(values[1:])


def _named_inputs(values: Sequence[str]) -> NamedInputSources:
    result: list[NamedInputSource] = []
    for token in values:
        name, separator, value = token.partition("=")
        if not separator:
            raise ValueError("named input must use name=value syntax")
        result.append(NamedInputSource(name, value))
    return tuple(result)


def _assignment(value: str, *, command: str) -> tuple[str, str]:
    field, separator, raw = value.partition("=")
    field = field.strip()
    raw = raw.strip()
    if not separator or not field or not raw:
        raise ValueError(f"{command} expects one field=value assignment")
    return field, raw


def _allow_value(
    field: str,
    raw_values: Sequence[str],
) -> tuple[str, ...] | None:
    values = split_selector_list(raw_values)
    if not values:
        raise ValueError(f"allow {field} requires selectors, all, or none")
    lowered = tuple(value.lower() for value in values)
    if lowered == ("all",):
        return None
    if lowered == ("none",):
        return ()
    if any(value in {"all", "none"} for value in lowered):
        raise ValueError(f"allow {field} cannot mix selectors with all or none")
    normalized = tuple(dict.fromkeys(values))
    domain = "model" if field == "models" else "tool" if field == "tools" else "cap"
    implicit_family = _CAP_KIND_BY_FIELD.get(field)
    for selector in normalized:
        parse_selector(selector, domain=domain, implicit_family=implicit_family)
    return normalized


def _default_value(field: str, raw: str) -> str | None:
    value = raw.strip()
    if not value:
        raise ValueError(f"default {field} must not be empty")
    if value.lower() == "none":
        return None
    if field == "model":
        parse_selector(value, domain="model")
    else:
        RunBindings(runnable=value)
    return value


def _limit_value(field: str, raw: str) -> int | Decimal | None:
    value = raw.strip()
    if not value:
        raise ValueError(f"limit {field} must not be empty")
    if value.lower() == "none":
        return None
    if field == "cost":
        try:
            parsed = Decimal(value)
        except InvalidOperation as error:
            raise ValueError("limit cost expects a decimal or none") from error
        if not parsed.is_finite() or parsed < 0:
            raise ValueError("limit cost expects a non-negative decimal or none")
        return parsed
    try:
        parsed = int(value)
    except ValueError as error:
        raise ValueError(f"limit {field} expects an integer or none") from error
    if parsed < 0:
        raise ValueError(f"limit {field} expects a non-negative integer or none")
    return parsed


def _normalize_commands(
    commands: Sequence[RunOverride],
) -> tuple[RunOverride, ...]:
    result: list[RunOverride] = []
    positions: dict[tuple[str, str], int] = {}
    for command in commands:
        key = (command.group, command.field)
        position = positions.get(key)
        if position is None:
            positions[key] = len(result)
            result.append(command)
            continue
        if command.group != "allow":
            raise ValueError(f"duplicate {command.group} field: {command.field}")
        previous = result[position]
        if not isinstance(previous.value, tuple) or not isinstance(
            command.value, tuple
        ):
            raise ValueError(f"allow {command.field} cannot combine selectors with all")
        if not previous.value or not command.value:
            raise ValueError(
                f"allow {command.field} cannot combine selectors with none"
            )
        result[position] = RunOverride(
            "allow",
            command.field,
            tuple(dict.fromkeys((*previous.value, *command.value))),
        )
    return tuple(result)


def _apply_binding_commands(
    current: RunBindings,
    base: RunBindings,
    commands: Sequence[RunOverride],
) -> RunBindings:
    fields: dict[str, str | None] = {
        "model": current.model,
        "runnable": current.runnable,
    }
    for command in commands:
        if command.group != "default":
            continue
        value = command.value
        if value is not None and not isinstance(value, str):
            raise TypeError(f"default {command.field} must be a string or none")
        fields[command.field] = getattr(base, command.field) if value is None else value
    return RunBindings(**fields)


def _apply_limit_commands(
    current: RunLimits,
    commands: Sequence[RunOverride],
) -> RunLimits:
    fields: dict[str, object] = {}
    for command in commands:
        if command.group == "limit":
            fields[command.field] = command.value
    return replace(current, **fields)


def _command_agent_ceiling(
    commands: Sequence[RunOverride],
) -> AgentCeiling | None:
    fields: dict[str, tuple[str, ...]] = {}
    present: set[str] = set()
    for command in commands:
        if command.group != "allow":
            continue
        value = command.value
        if value is None:
            continue
        elif isinstance(value, tuple):
            present.add(command.field)
            fields[command.field] = value
        else:
            raise TypeError(f"allow {command.field} must be selectors, all, or none")

    models = fields.get("models") if "models" in present else None
    tools = fields.get("tools") if "tools" in present else None
    cap_present = bool(present & {"caps", *_CAP_KIND_BY_FIELD})
    caps: list[str] = list(fields.get("caps", ()))
    for plural, kind in _CAP_KIND_BY_FIELD.items():
        caps.extend(
            _qualify_cap_selector(kind, value) for value in fields.get(plural, ())
        )
    ceiling = AgentCeiling(
        models=models,
        tools=tools,
        caps=tuple(caps) if cap_present else None,
    )
    if ceiling.models is None and ceiling.tools is None and ceiling.caps is None:
        return None
    return ceiling


def _qualify_cap_selector(kind: str, value: str) -> str:
    parsed = parse_selector(value, domain="cap", implicit_family=kind)
    text = value.strip()
    suffix = text[text.find("[") :] if "[" in text else ""
    return f"{kind}/{parsed.pattern}{suffix}"


class _Line:
    __slots__ = ("text", "start")

    def __init__(self, text: str, start: int) -> None:
        self.text = text
        self.start = start


def _lines(source: str) -> tuple[_Line, ...]:
    lines: list[_Line] = []
    start = 0
    for value in source.splitlines(keepends=True):
        text = value.removesuffix("\n").removesuffix("\r")
        lines.append(_Line(text, start))
        start += len(value)
    if source and (not lines or start < len(source)):
        lines.append(_Line(source[start:], start))
    return tuple(lines)


def _is_blank(value: str) -> bool:
    return not value.strip(" \t")
