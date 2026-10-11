"""Parse submission envelopes and setting bodies without runtime resolution."""

from __future__ import annotations

from collections.abc import Sequence
import math
import shlex
from typing import cast

from toolang.base.errors import ToolangError
from toolang.base.money import normalize_cost
from toolang.base.model_settings import parse_model_body
from toolang.base.types.model import ModelOverride
from toolang.common.policy import resolve_query_sentinels
from toolang.lang.input import (
    CallInput,
    CallInputHeader,
    capture_call_input,
    parse_call_input_header,
    parse_input,
    validate_runnable_input_names,
)
from toolang.lang.types import parse_runnable_ref_parts

from .types import (
    ALLOW_FIELDS,
    LIMIT_FIELDS,
    AllowField,
    AllowOverride,
    LimitField,
    LimitOverride,
    RunOverride,
)


# Shared input vocabulary; Chat owns handlers, while run surfaces reject commands.
SLASH_COMMAND_NAMES = frozenset(
    {
        "help",
        "?",
        "keys",
        "models",
        "tools",
        "caps",
        "agics",
        "flows",
        "output",
        "show",
        "exit",
        "quit",
    }
)
RUN_OVERRIDE_NAMES = frozenset({"model", "allow", "limit", "workdir"})
SETTING_NAMES = frozenset(
    {"model", "agic", "flow", "runnable", "workdir", "allow", "limit"}
)
COLON_COMMAND_NAMES = SETTING_NAMES | {"?"}


def parse_call(source: str) -> tuple[RunOverride, CallInput[str]]:
    """Parse one run-only source into an aggregate override and runnable input."""

    body = _strip_final_line_break(source)
    override, call_input = parse_run_prefix(body)
    return override, parse_input(call_input)


def parse_slash_directive(line: str) -> tuple[RunOverride, CallInput[str]]:
    """Parse one slash run directive without capturing primary input."""

    try:
        parsed = _try_parse_slash_directive(line)
    except ToolangError as error:
        raise ValueError(str(error)) from error
    if parsed is None:
        raise ValueError("line is not a slash directive")
    override, named, header = parsed
    if header is not None and header.form is not None:
        raise ValueError("slash directive parsing does not accept attached call input")
    return override, named


def parse_setting_override(command: str, body: str) -> RunOverride:
    """Parse one colon setting body using the shared override grammar."""

    if command not in SETTING_NAMES:
        raise ValueError(f"unknown setting command: :{command}")
    try:
        tokens = shlex.split(body, comments=False, posix=True)
    except ValueError as error:
        raise ValueError(str(error)) from error
    if command == "model":
        return RunOverride(model=parse_model_body(body))
    if command in {"runnable", "agic", "flow"}:
        override, named = _session_runnable_override(command, tokens)
        if named:
            raise ValueError(f":{command} does not accept named runnable input")
        return override
    if command == "workdir":
        if len(tokens) != 1:
            raise ValueError("workdir requires exactly one path")
        return RunOverride(workdir=tokens[0])
    if command == "allow":
        return _allow_override(tokens, command=":allow")
    return _limit_override(tokens, command=":limit")


def parse_run_prefix(
    source: str,
) -> tuple[RunOverride, CallInput[str]]:
    """Parse leading run overrides, one optional root call, and its input."""

    lines = _lines(source)
    overrides: list[RunOverride] = []
    named: dict[str, str] = {}
    index = 0
    has_prefix = False
    while index < len(lines):
        line = lines[index]
        if _is_blank(line.text):
            if not overrides:
                input = CallInput({"_": source[line.start :]})
                break
            index += 1
            continue
        if line.text.startswith(":") and not line.text.startswith("::"):
            raise ValueError("colon commands cannot be used in a run submission")
        try:
            parsed = _try_parse_slash_directive(line.text)
        except ToolangError as error:
            raise ValueError(str(error)) from error
        if parsed is None:
            input = CallInput({"_": source[line.start :]})
            break
        override, call_named, header = parsed
        overrides.append(override)
        index += 1
        if header is None:
            has_prefix = True
            continue
        named.update(call_named)
        following = source[lines[index].start :] if index < len(lines) else ""
        if header.form is None:
            while index < len(lines) and _is_blank(lines[index].text):
                index += 1
            primary = source[lines[index].start :] if index < len(lines) else None
        else:
            try:
                primary, trailing = capture_call_input(
                    header, following, label="runnable call", root=True
                )
            except ToolangError as error:
                raise ValueError(str(error)) from error
            if trailing.strip():
                raise ValueError(
                    "Root line input for runnable call cannot be followed by other content."
                )
        input = CallInput({"_": primary, **named} if primary is not None else named)
        break
    else:
        input = CallInput(named)

    if has_prefix and not input:
        raise ValueError("run override prefix requires primary or named input")
    return merge_run_overrides(overrides), input


def merge_run_overrides(overrides: Sequence[RunOverride]) -> RunOverride:
    """Combine a complete authored override section and reject duplicates."""

    model: ModelOverride | None = None
    runnable: str | None = None
    workdir: str | None = None
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
        if override.workdir is not None:
            if workdir is not None:
                raise ValueError("duplicate workdir override")
            workdir = override.workdir
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
        workdir=workdir,
    )


def _try_parse_slash_directive(
    line: str,
) -> tuple[RunOverride, CallInput[str], CallInputHeader | None] | None:
    if not line.startswith("/") or line.startswith("//"):
        return None
    head, *parts = line.split(maxsplit=1)
    name = head[1:]
    body = parts[0] if parts else ""
    if not name:
        raise ValueError("a slash directive requires a name immediately after /")
    if name in SLASH_COMMAND_NAMES:
        raise ValueError(f"/{name} is a standalone Chat command")
    if name in RUN_OVERRIDE_NAMES:
        if name == "model":
            override = RunOverride(model=parse_model_body(body))
        else:
            tokens = shlex.split(body, comments=False, posix=True)
            if name == "workdir":
                if len(tokens) != 1 or not tokens[0]:
                    raise ValueError("/workdir requires exactly one path")
                override = RunOverride(workdir=tokens[0])
            elif name == "allow":
                override = _allow_override(tokens)
            else:
                override = _limit_override(tokens)
        return override, CallInput(), None

    parse_runnable_ref_parts(name)
    header = parse_call_input_header(body, label=f"/{name} runnable call")
    tokens = shlex.split(header.arguments, comments=False, posix=True)
    return RunOverride(runnable=name), _named_inputs(tokens), header


def _session_runnable_override(
    name: str,
    tokens: Sequence[str],
) -> tuple[RunOverride, CallInput[str]]:
    if not tokens:
        raise ValueError(f":{name} requires a runnable identity")
    target = tokens[0]
    if name == "runnable":
        runnable = "default" if target == "default" else target
    else:
        runnable = f"{name}:{target}"
    if runnable != "default":
        parse_runnable_ref_parts(runnable)
    return RunOverride(runnable=runnable), _named_inputs(tokens[1:])


def _allow_override(tokens: Sequence[str], *, command: str = "/allow") -> RunOverride:
    if not tokens:
        raise ValueError(f"{command} requires at least one field=value assignment")
    values: dict[str, tuple[str, ...] | None] = {}
    order: list[str] = []
    for token in tokens:
        field, raw = _assignment(token, command=command)
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


def _limit_override(tokens: Sequence[str], *, command: str = "/limit") -> RunOverride:
    if not tokens:
        raise ValueError(f"{command} requires at least one field=value assignment")
    values: list[LimitOverride] = []
    present: set[str] = set()
    for token in tokens:
        field, raw = _assignment(token, command=command)
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


def _named_inputs(values: Sequence[str]) -> CallInput[str]:
    result: dict[str, str] = {}
    for token in values:
        name, separator, value = token.partition("=")
        if not separator:
            raise ValueError("named input must use name=value syntax")
        if name == "_":
            raise ValueError("primary input must use the input capture syntax")
        if name in result:
            raise ValueError(f"duplicate named input: {name}")
        result[name] = value
    validate_runnable_input_names(result)
    return CallInput(result)


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
    return normalized


def _limit_value(field: str, raw: str) -> int | float | None:
    value = raw.strip()
    if value.lower() == "none":
        return None
    if field == "cost":
        try:
            parsed = float(value)
        except ValueError as error:
            raise ValueError("limit cost expects a decimal or none") from error
        if not math.isfinite(parsed) or parsed < 0:
            raise ValueError("limit cost expects a non-negative decimal or none")
        return normalize_cost(parsed)
    try:
        parsed = int(value)
    except ValueError as error:
        raise ValueError(f"limit {field} expects an integer or none") from error
    if parsed < 0:
        raise ValueError(f"limit {field} expects a non-negative integer or none")
    return parsed


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


def _strip_final_line_break(source: str) -> str:
    if source.endswith("\r\n"):
        return source[:-2]
    if source.endswith("\n"):
        return source[:-1]
    return source
