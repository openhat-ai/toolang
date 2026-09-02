"""Parse and materialize session settings and input-local run overrides."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from decimal import Decimal, InvalidOperation
import re
import shlex
from types import MappingProxyType
from typing import cast

from toolang.base.errors import ToolangError
from toolang.base.types.model import (
    ModelRequest,
    ReasoningEffort,
    ReasoningParameters,
)
from toolang.base.types.policy import AgentCeiling, RunBindings, RunLimits
from toolang.common.query import resolve_query_sentinels
from toolang.lang.input import (
    CallInput,
    CallInputHeader,
    NamedInputSource,
    NamedInputSources,
    capture_call_input,
    parse_call_input_header,
)
from toolang.lang.runnable_query import RUNNABLE_SCHEMA
from toolang.plugin.models.collections import MODEL_SCHEMA
from toolang.plugin.toolsets.collections import TOOL_SCHEMA
from toolang.setup import AgentSetup
from toolang.state.collections import cap_kind_definition
from toolang.state.types import EntryKind

from .types import (
    ALLOW_FIELDS,
    LIMIT_FIELDS,
    AllowField,
    AllowOverride,
    LimitField,
    LimitOverride,
    ModelEffort,
    ModelOverride,
    RunCommand,
    RunOverride,
    SessionSetting,
)

_CAP_KIND_BY_FIELD = {
    "psyches": "psyche",
    "skills": "skill",
    "services": "service",
    "prompts": "prompt",
}
_BUDGET_RE = re.compile(r"0|[1-9][0-9]*\Z")
_REASONING_EFFORTS = frozenset(
    {"none", "minimal", "low", "medium", "high", "xhigh", "max", "default"}
)
# Setting name -> (standalone setting body, independently useful override bodies).
SETTING_OVERRIDE_FORMS: Mapping[str, tuple[str, tuple[str, ...]]] = MappingProxyType(
    {
        "model": ("[MODEL] [effort=VALUE]", ("MODEL", "effort=VALUE")),
        "agic": ("AGIC", ("AGIC",)),
        "flow": ("FLOW", ("FLOW",)),
        "runnable": ("RUNNABLE", ("RUNNABLE",)),
        "allow": ("FIELD=QUERY...", ("FIELD=QUERY...",)),
        "limit": ("FIELD=VALUE...", ("FIELD=VALUE...",)),
    }
)


def parse_run_override(line: str) -> tuple[RunOverride, NamedInputSources]:
    """Parse one leading colon override."""

    try:
        parsed = _try_parse_override(line)
    except ToolangError as error:
        raise ValueError(str(error)) from error
    if parsed is None:
        raise ValueError("line is not a run override")
    override, named, header = parsed
    if header is not None and header.form is not None:
        raise ValueError("run override parsing does not accept attached call input")
    return override, named


def parse_setting_override(command: str, body: str) -> RunOverride:
    """Parse one slash setting body using the shared override grammar."""

    if command not in SETTING_OVERRIDE_FORMS:
        raise ValueError(f"unknown setting command: /{command}")
    try:
        tokens = shlex.split(body, comments=False, posix=True)
    except ValueError as error:
        raise ValueError(str(error)) from error
    if command == "model":
        return _model_override(tokens)
    if command in {"runnable", "agic", "flow"}:
        override, named = _runnable_override(command, tokens)
        if named:
            raise ValueError(f"/{command} does not accept named runnable input")
        return override
    if command == "allow":
        return _allow_override(tokens)
    return _limit_override(tokens)


def parse_policy_prefix(
    source: str,
) -> tuple[RunOverride, CallInput]:
    """Parse one complete leading override section and its remaining source."""

    lines = _lines(source)
    overrides: list[RunOverride] = []
    named: list[NamedInputSource] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if _is_blank(line.text):
            if not overrides:
                break
            index += 1
            continue
        try:
            parsed = _try_parse_override(line.text)
        except ToolangError as error:
            raise ValueError(str(error)) from error
        if parsed is None:
            break
        override, override_named, call_header = parsed
        overrides.append(override)
        named.extend(override_named)
        index += 1
        if call_header is not None and call_header.form is not None:
            following = source[lines[index].start :] if index < len(lines) else ""
            try:
                primary, trailing = capture_call_input(
                    call_header,
                    following,
                    label="runnable call",
                    root=True,
                )
            except ToolangError as error:
                raise ValueError(str(error)) from error
            if call_header.form == "line" and trailing.strip():
                raise ValueError(
                    "Root line input for runnable call cannot be followed by "
                    "other content."
                )
            return (
                merge_run_overrides(overrides),
                CallInput(_=primary, named=tuple(named)),
            )

    remaining = source[lines[index].start :] if index < len(lines) else ""
    return (
        merge_run_overrides(overrides),
        CallInput(_=remaining or None, named=tuple(named)),
    )


def merge_run_overrides(overrides: Sequence[RunOverride]) -> RunOverride:
    """Combine a complete authored override section and reject duplicates."""

    model: ModelOverride | None = None
    runnable: str | None = None
    allow: list[AllowOverride] = []
    allow_positions: dict[AllowField, int] = {}
    limits: list[LimitOverride] = []
    limit_fields: set[str] = set()
    for override in overrides:
        if override.model is not None:
            if model is not None:
                raise ValueError("duplicate model override")
            model = override.model
        if override.runnable is not None:
            if runnable is not None:
                raise ValueError("duplicate runnable override")
            runnable = override.runnable
        for item in override.allow:
            position = allow_positions.get(item.field)
            if position is None:
                allow_positions[item.field] = len(allow)
                allow.append(item)
                continue
            previous = allow[position]
            allow[position] = AllowOverride(
                item.field,
                _merge_allow_value(item.field, previous.value, item.value),
            )
        for item in override.limits:
            if item.field in limit_fields:
                raise ValueError(f"duplicate limit field: {item.field}")
            limit_fields.add(item.field)
            limits.append(item)
    return RunOverride(
        model=model,
        runnable=runnable,
        allow=tuple(allow),
        limits=tuple(limits),
    )


def merge_commands(
    current: Sequence[RunCommand],
    updates: Sequence[RunCommand],
) -> tuple[RunCommand, ...]:
    """Apply low-level updates to a compact retained command sequence."""

    merged = list(_normalize_commands(current))
    for command in _normalize_commands(updates):
        key = (command.group, command.field)
        merged = [item for item in merged if (item.group, item.field) != key]
        if command.value is None and command.group in {"allow", "default"}:
            continue
        merged.append(command)
    return tuple(merged)


def commands_from_run_override(override: RunOverride) -> tuple[RunCommand, ...]:
    """Lower policy-compatible authored fields for durable execution provenance."""

    commands: list[RunCommand] = []
    if override.model is not None and override.model.identity not in {None, "unset"}:
        commands.append(
            RunCommand(
                "default",
                "model",
                None
                if override.model.identity == "default"
                else override.model.identity,
            )
        )
    if override.runnable is not None:
        commands.append(
            RunCommand(
                "default",
                "runnable",
                None if override.runnable == "default" else override.runnable,
            )
        )
    commands.extend(
        RunCommand("allow", item.field, item.value) for item in override.allow
    )
    commands.extend(
        RunCommand("limit", item.field, item.value) for item in override.limits
    )
    return tuple(commands)


def apply_session_setting(
    surface: SessionSetting,
    current: SessionSetting,
    update: RunOverride,
) -> SessionSetting:
    """Apply one validated slash setting body atomically to a session."""

    model = _apply_model_override(current.model, surface.model, update.model)
    runnable = current.runnable
    if update.runnable is not None:
        runnable = surface.runnable if update.runnable == "default" else update.runnable
    allow = _replace_allow_fields(current.allow, update)
    limits = _apply_limit_overrides(current.limits, update.limits)
    return SessionSetting(model=model, runnable=runnable, allow=allow, limits=limits)


def materialize_run_setting(
    surface: SessionSetting,
    session: SessionSetting,
    override: RunOverride,
) -> tuple[tuple[AgentCeiling, ...], SessionSetting]:
    """Materialize one input-local override over concrete session settings."""

    model = _apply_model_override(session.model, surface.model, override.model)
    runnable = session.runnable
    if override.runnable is not None:
        runnable = (
            surface.runnable if override.runnable == "default" else override.runnable
        )
    limits = _apply_limit_overrides(session.limits, override.limits)
    run_ceiling = _allow_ceiling(override.allow)
    ceilings = tuple(
        ceiling
        for ceiling in (session.allow, run_ceiling)
        if ceiling is not None and _ceiling_restricts(ceiling)
    )
    return ceilings, SessionSetting(
        model=model,
        runnable=runnable,
        allow=session.allow,
        limits=limits,
    )


def resolve_commands(
    setup: AgentSetup,
    *,
    surface: RunBindings = RunBindings(),
    session: Sequence[RunCommand] = (),
    run: Sequence[RunCommand] = (),
) -> tuple[tuple[AgentCeiling, ...], RunBindings, RunLimits]:
    """Resolve retained low-level command layers for execution protocols."""

    base = RunBindings(
        model=surface.model if surface.model is not None else setup.defaults.model,
        runnable=(
            surface.runnable
            if surface.runnable is not None
            else setup.defaults.runnable
        ),
    )
    return materialize_policy(base, setup.limits, session=session, run=run)


def materialize_policy(
    defaults: RunBindings,
    default_limits: RunLimits,
    *,
    session: Sequence[RunCommand] = (),
    run: Sequence[RunCommand] = (),
) -> tuple[tuple[AgentCeiling, ...], RunBindings, RunLimits]:
    """Materialize retained low-level command layers over concrete defaults."""

    bindings = _apply_binding_commands(defaults, defaults, session)
    bindings = _apply_binding_commands(bindings, defaults, run)
    limits = _apply_command_limits(default_limits, session)
    limits = _apply_command_limits(limits, run)
    ceilings = tuple(
        ceiling
        for commands in (session, run)
        if (ceiling := _command_agent_ceiling(commands)) is not None
    )
    return ceilings, bindings, limits


def _try_parse_override(
    line: str,
) -> tuple[RunOverride, NamedInputSources, CallInputHeader | None] | None:
    if not line.startswith(":") or line.startswith("::"):
        return None
    try:
        tokens = shlex.split(line, comments=False, posix=True)
    except ValueError as error:
        raise ValueError(str(error)) from error
    if not tokens:
        return None
    name = tokens[0][1:]
    body = tokens[1:]
    if name not in SETTING_OVERRIDE_FORMS:
        return None
    if name == "model":
        return _model_override(body), (), None
    if name in {"runnable", "agic", "flow"}:
        raw_body = line[len(tokens[0]) :].lstrip(" \t")
        header = parse_call_input_header(raw_body, label=f":{name} runnable call")
        try:
            runnable_tokens = shlex.split(header.arguments, comments=False, posix=True)
        except ValueError as error:
            raise ValueError(str(error)) from error
        override, named = _runnable_override(name, runnable_tokens)
        return override, named, header
    if name == "allow":
        return _allow_override(body), (), None
    if name == "limit":
        return _limit_override(body), (), None
    raise AssertionError(f"unhandled setting override: {name}")


def _model_override(tokens: Sequence[str]) -> RunOverride:
    if not tokens:
        raise ValueError(":model requires an identity or effort assignment")
    identity: str | None = None
    effort: ModelEffort | None = None
    for index, token in enumerate(tokens):
        if "=" not in token:
            if index != 0 or identity is not None:
                raise ValueError("model identity must be the first token")
            sentinel = token.lower()
            if sentinel == "none":
                raise ValueError(
                    "model identity 'none' was removed; use :model unset for "
                    "a model-free run"
                )
            identity = sentinel if sentinel in {"default", "unset"} else token
            if identity not in {"default", "unset"}:
                ModelRequest(identity)
            continue
        field, raw = _assignment(token, command=":model")
        if field != "effort":
            raise ValueError(f"unknown model parameter: {field}")
        if effort is not None:
            raise ValueError("duplicate model parameter: effort")
        effort = _effort_value(raw)
    return RunOverride(
        model=ModelOverride(
            identity=identity,
            effort=effort,
        )
    )


def _runnable_override(
    name: str,
    tokens: Sequence[str],
) -> tuple[RunOverride, NamedInputSources]:
    if not tokens:
        raise ValueError(f":{name} requires a runnable identity")
    target = tokens[0]
    if name == "runnable":
        runnable = "default" if target == "default" else target
    else:
        runnable = f"{name}:{target}"
    if runnable != "default":
        RUNNABLE_SCHEMA.parse(runnable)
    return RunOverride(runnable=runnable), _named_inputs(tokens[1:])


def _allow_override(tokens: Sequence[str]) -> RunOverride:
    if not tokens:
        raise ValueError(":allow requires at least one field=value assignment")
    values: dict[str, tuple[str, ...] | None] = {}
    order: list[str] = []
    for token in tokens:
        field, raw = _assignment(token, command=":allow")
        if field not in ALLOW_FIELDS:
            raise ValueError(f"unknown allow field: {field}")
        parsed = _allow_value(field, (raw,))
        if field not in values:
            values[field] = parsed
            order.append(field)
            continue
        values[field] = _merge_allow_value(field, values[field], parsed)
    return RunOverride(
        allow=tuple(
            AllowOverride(cast(AllowField, field), values[field]) for field in order
        )
    )


def _limit_override(tokens: Sequence[str]) -> RunOverride:
    if not tokens:
        raise ValueError(":limit requires at least one field=value assignment")
    values: list[LimitOverride] = []
    present: set[str] = set()
    for token in tokens:
        field, raw = _assignment(token, command=":limit")
        if field not in LIMIT_FIELDS:
            raise ValueError(f"unknown run limit: {field}")
        if field in present:
            raise ValueError(f"duplicate limit field: {field}")
        present.add(field)
        values.append(
            LimitOverride(
                cast(LimitField, field),
                _limit_value(field, raw),
            )
        )
    return RunOverride(limits=tuple(values))


def _effort_value(raw: str) -> ModelEffort:
    if raw == "auto":
        return "auto"
    if _BUDGET_RE.fullmatch(raw):
        return int(raw)
    if raw in _REASONING_EFFORTS:
        return cast(ReasoningEffort, raw)
    raise ValueError(f"unknown reasoning effort: {raw!r}")


def _apply_model_override(
    current: ModelRequest | None,
    surface: ModelRequest | None,
    override: ModelOverride | None,
) -> ModelRequest | None:
    if override is None:
        return current
    if override.identity == "default":
        model = surface
    elif override.identity == "unset":
        model = None
    elif override.identity is not None:
        model = ModelRequest(override.identity)
    else:
        model = current
    if override.effort is None:
        return model
    if model is None:
        raise ValueError("model effort requires an effective model")
    if override.effort == "auto":
        reasoning = None
    elif isinstance(override.effort, int):
        reasoning = ReasoningParameters(budget_tokens=override.effort)
    else:
        reasoning = ReasoningParameters(effort=override.effort)
    return replace(
        model,
        parameters=replace(model.parameters, reasoning=reasoning),
    )


def _replace_allow_fields(current: AgentCeiling, update: RunOverride) -> AgentCeiling:
    if not update.allow:
        return current
    return replace(
        current,
        **{item.field: item.value for item in update.allow},
    )


def _apply_limit_overrides(
    current: RunLimits,
    overrides: Sequence[LimitOverride],
) -> RunLimits:
    return replace(current, **{item.field: item.value for item in overrides})


def _apply_binding_commands(
    current: RunBindings,
    base: RunBindings,
    commands: Sequence[RunCommand],
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


def _normalize_commands(commands: Sequence[RunCommand]) -> tuple[RunCommand, ...]:
    result: list[RunCommand] = []
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
            raise ValueError(f"allow {command.field} cannot combine queries with all")
        if not previous.value or not command.value:
            raise ValueError(f"allow {command.field} cannot combine queries with none")
        result[position] = RunCommand(
            "allow",
            command.field,
            tuple(dict.fromkeys((*previous.value, *command.value))),
        )
    return tuple(result)


def _apply_command_limits(
    current: RunLimits,
    commands: Sequence[RunCommand],
) -> RunLimits:
    fields = {
        command.field: command.value for command in commands if command.group == "limit"
    }
    return replace(current, **fields)


def _command_agent_ceiling(
    commands: Sequence[RunCommand],
) -> AgentCeiling | None:
    fields: dict[str, tuple[str, ...]] = {}
    present: set[str] = set()
    for command in commands:
        if command.group != "allow" or command.value is None:
            continue
        if not isinstance(command.value, tuple):
            raise TypeError(f"allow {command.field} must be queries, all, or none")
        try:
            normalized = resolve_query_sentinels(
                command.value,
                label=f"allow {command.field}",
            )
        except ToolangError as error:
            raise ValueError(str(error)) from error
        if normalized is None:
            continue
        present.add(command.field)
        fields[command.field] = normalized
    ceiling = AgentCeiling(
        **{
            field: fields.get(field) if field in present else None
            for field in ALLOW_FIELDS
        }
    )
    return ceiling if _ceiling_restricts(ceiling) else None


def _allow_ceiling(overrides: Sequence[AllowOverride]) -> AgentCeiling | None:
    if not overrides:
        return None
    return AgentCeiling(**{item.field: item.value for item in overrides})


def _merge_allow_value(
    field: str,
    current: tuple[str, ...] | None,
    update: tuple[str, ...] | None,
) -> tuple[str, ...] | None:
    if current == update:
        return current
    if current is None or update is None:
        raise ValueError(f"allow {field} cannot combine queries with all")
    if not current or not update:
        raise ValueError(f"allow {field} cannot combine queries with none")
    return tuple(dict.fromkeys((*current, *update)))


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
        raise ValueError(f"{command} expects field=value assignments")
    return field, raw


def _allow_value(
    field: str,
    raw_values: Sequence[str],
) -> tuple[str, ...] | None:
    values = tuple(value.strip() for value in raw_values if value.strip())
    if not values:
        raise ValueError(f"allow {field} requires queries, all, or none")
    try:
        normalized = resolve_query_sentinels(values, label=f"allow {field}")
    except ToolangError as error:
        raise ValueError(str(error)) from error
    if normalized is None or not normalized:
        return normalized
    schema = (
        MODEL_SCHEMA
        if field == "models"
        else TOOL_SCHEMA
        if field == "tools"
        else cap_kind_definition(cast(EntryKind, _CAP_KIND_BY_FIELD[field])).schema
    )
    for query in normalized:
        schema.parse(query)
    return normalized


def _limit_value(field: str, raw: str) -> int | Decimal | None:
    value = raw.strip()
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


def _ceiling_restricts(ceiling: AgentCeiling) -> bool:
    return any(getattr(ceiling, field) is not None for field in ALLOW_FIELDS)


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
