"""Semantic validation for lowered Toolang programs."""

from __future__ import annotations

import re
from typing import Any

from toolang.base.error import ToolangError

from .ast import CapDecl, Flow, Program, Thunk

SERVICE_FIELDS = frozenset({"description", "transport", "protocol", "target", "headers", "env"})
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def validate_program(program: Program) -> None:
    """Validate one lowered semantic AST as a Toolang program."""

    seen_cap_names: set[tuple[str, str]] = set()
    seen_context_names: set[str | None] = set()
    seen_instruct_names: set[str | None] = set()
    seen_struct_names: set[str] = set()
    seen_thunk_names: set[str] = set()
    seen_flow_names: set[str] = set()

    for cap in program.caps:
        cap_key = (cap.kind, cap.name)
        if cap_key in seen_cap_names:
            raise ToolangError(f"Duplicate {cap.kind} name {cap.name!r}.")
        seen_cap_names.add(cap_key)
        _validate_cap_metadata(cap)
        _validate_cap_params(cap)

    for context in program.contexts:
        if context.name in {"default", "none"}:
            raise ToolangError(f"Context name {context.name!r} is reserved.")
        if context.name in seen_context_names:
            label = "default" if context.name is None else context.name
            raise ToolangError(f"Duplicate context name {label!r}.")
        seen_context_names.add(context.name)

    for instruct in program.instructs:
        if instruct.name in {"default", "none"}:
            raise ToolangError(f"Instruct name {instruct.name!r} is reserved.")
        if instruct.name in seen_instruct_names:
            label = "default" if instruct.name is None else instruct.name
            raise ToolangError(f"Duplicate instruct name {label!r}.")
        seen_instruct_names.add(instruct.name)

    for struct in program.structs:
        if struct.name in seen_struct_names:
            raise ToolangError(f"Duplicate struct name {struct.name!r}.")
        seen_struct_names.add(struct.name)

    for thunk in program.thunks:
        thunk_name = _thunk_name(thunk)
        if thunk_name in seen_thunk_names:
            raise ToolangError(f"Duplicate thunk name {thunk_name!r}.")
        seen_thunk_names.add(thunk_name)
        _validate_thunk_params(thunk, thunk_name=thunk_name)
        _validate_thunk_directives(thunk, thunk_name=thunk_name)
        _validate_thunk_messages(thunk, thunk_name=thunk_name)

    for flow in program.flows:
        flow_name = flow.flow_name()
        if flow_name in seen_flow_names:
            raise ToolangError(f"Duplicate flow name {flow_name!r}.")
        seen_flow_names.add(flow_name)
        _validate_flow_params(flow, flow_name=flow_name)
        _validate_flow_directives(flow, flow_name=flow_name)


def validate_service_meta(
    meta: dict[str, Any],
    *,
    line_number: int,
    require_description: bool = False,
) -> None:
    _require_exact_fields(
        meta=meta,
        allowed=SERVICE_FIELDS,
        kind="service",
        line_number=line_number,
    )
    description = meta.get("description")
    if require_description and (not isinstance(description, str) or not description):
        raise ToolangError(f"Service cap at line {line_number} is missing description.")
    if description is not None and not isinstance(description, str):
        raise ToolangError(f"Service cap at line {line_number} must define description as a string.")
    transport = meta.get("transport") or meta.get("protocol")
    if not isinstance(transport, str) or not transport:
        raise ToolangError(f"Service cap at line {line_number} is missing protocol.")
    if transport not in {"http", "stdio"}:
        raise ToolangError(
            f"Service cap at line {line_number} uses unsupported transport {transport!r}."
        )
    target = meta.get("target")
    if not isinstance(target, str) or not target:
        raise ToolangError(f"Service cap at line {line_number} is missing target.")
    headers = meta.get("headers")
    if headers is not None and not isinstance(headers, str) and not _is_string_map(headers):
        raise ToolangError(
            f"Service cap at line {line_number} must define headers as a string map."
        )
    env = meta.get("env")
    if env is not None and not _is_env_names(env):
        raise ToolangError(
            f"Service cap at line {line_number} must list environment variable names."
        )


def _validate_cap_params(cap: CapDecl) -> None:
    seen: set[str] = set()
    for param in cap.params:
        if param.name in seen:
            raise ToolangError(
                f"Duplicate prompt parameter {param.name!r} in {cap.kind} {cap.name}."
            )
        seen.add(param.name)


def _validate_cap_metadata(cap: CapDecl) -> None:
    if cap.kind == "service":
        validate_service_meta(
            cap.meta,
            line_number=cap.span.line,
            require_description=True,
        )


def _validate_thunk_params(thunk: Thunk, *, thunk_name: str) -> None:
    if thunk.input is not None and thunk.input.name == "runtime":
        raise ToolangError(f"Thunk {thunk_name!r} must not use reserved parameter name 'runtime'.")
    seen: set[str] = set()
    for param in thunk.params:
        if param.name == "runtime":
            raise ToolangError(f"Thunk {thunk_name!r} must not use reserved parameter name 'runtime'.")
        if param.name in seen:
            raise ToolangError(f"Duplicate thunk parameter {param.name!r} in {thunk_name!r}.")
        seen.add(param.name)


def _validate_thunk_directives(thunk: Thunk, *, thunk_name: str) -> None:
    model_directives = [directive for directive in thunk.directives if _directive_family(directive.name) == "model"]
    if len(model_directives) > 1:
        raise ToolangError(f"Thunk {thunk_name!r} may declare at most one models directive.")
    if model_directives:
        directive = model_directives[0]
        if directive.operator != "=":
            raise ToolangError(f"Thunk {thunk_name!r} must use '=' for its models directive.")
        if not directive.values:
            raise ToolangError(f"Thunk {thunk_name!r} must declare at least one model selector.")
        routed = [selector for selector in directive.values if "@" in selector]
        if routed:
            joined = ", ".join(routed)
            raise ToolangError(
                f"Thunk {thunk_name!r} must declare route-neutral model refs, not routed selectors: {joined}"
            )

    recall_directives = [directive for directive in thunk.directives if _directive_family(directive.name) == "recall"]
    if len(recall_directives) > 1:
        raise ToolangError(f"Thunk {thunk_name!r} may declare at most one recall directive.")
    if not recall_directives:
        return
    recall = recall_directives[0]
    if recall.operator != "=":
        raise ToolangError(f"Thunk {thunk_name!r} must use '=' for its recall directive.")
    values = set(recall.values)
    if not values:
        raise ToolangError(f"Thunk {thunk_name!r} must declare at least one recall source.")
    if values in ({"none"}, {"default"}, {"history"}, {"memory"}, {"history", "memory"}):
        return
    joined = ", ".join(recall.values)
    raise ToolangError(f"Thunk {thunk_name!r} has unsupported recall directive values: {joined}.")


def _validate_thunk_messages(thunk: Thunk, *, thunk_name: str) -> None:
    instruct_count = len(thunk.message_blocks("instruct"))
    if instruct_count > 1:
        raise ToolangError(f"Thunk {thunk_name!r} may declare at most one instruct block.")
    context_count = len(thunk.message_blocks("context"))
    if context_count > 1:
        raise ToolangError(f"Thunk {thunk_name!r} may declare at most one context block.")
    unsupported = [block.kind for block in thunk.messages if block.kind not in {"user", "assistant", "tool"}]
    if unsupported:
        joined = ", ".join(unsupported)
        raise ToolangError(
            f"Thunk {thunk_name!r} does not yet support message blocks: {joined}."
        )


def _validate_flow_params(flow: Flow, *, flow_name: str) -> None:
    seen: set[str] = set()
    if flow.input is not None and flow.input.name in {"runtime"}:
        raise ToolangError(f"Flow {flow_name!r} must not use reserved parameter name 'runtime'.")
    for param in flow.params:
        if param.name == "runtime":
            raise ToolangError(f"Flow {flow_name!r} must not use reserved parameter name 'runtime'.")
        if param.name in seen:
            raise ToolangError(f"Duplicate flow parameter {param.name!r} in {flow_name!r}.")
        seen.add(param.name)


def _validate_flow_directives(flow: Flow, *, flow_name: str) -> None:
    model_directives = [directive for directive in flow.directives if _directive_family(directive.name) == "model"]
    if len(model_directives) > 1:
        raise ToolangError(f"Flow {flow_name!r} may declare at most one models directive.")
    recall_directives = [directive for directive in flow.directives if _directive_family(directive.name) == "recall"]
    if len(recall_directives) > 1:
        raise ToolangError(f"Flow {flow_name!r} may declare at most one recall directive.")


def _require_exact_fields(
    *,
    meta: dict[str, Any],
    allowed: frozenset[str],
    kind: str,
    line_number: int,
) -> None:
    unknown = sorted(set(meta) - allowed)
    if unknown:
        joined = ", ".join(unknown)
        raise ToolangError(f"{kind.capitalize()} at line {line_number} has unsupported field(s): {joined}.")


def _is_string_map(value: object) -> bool:
    return isinstance(value, dict) and all(
        isinstance(key, str) and isinstance(item, str)
        for key, item in value.items()
    )


def _is_env_names(value: object) -> bool:
    if isinstance(value, str):
        items = [item.strip() for item in value.split(",")]
    elif isinstance(value, list | tuple):
        items = [item.strip() for item in value if isinstance(item, str)]
        if len(items) != len(value):
            return False
    else:
        return False
    return bool(items) and all(ENV_NAME_RE.fullmatch(item) is not None for item in items)


def _thunk_name(thunk: Thunk) -> str:
    return thunk.name or "main"


def _directive_family(name: str) -> str:
    normalized = name.strip()
    if normalized in {"model", "models"}:
        return "model"
    if normalized in {"tool", "tools"}:
        return "tool"
    if normalized in {"psyche", "psyches"}:
        return "psyche"
    if normalized in {"skill", "skills"}:
        return "skill"
    if normalized in {"service", "services"}:
        return "service"
    if normalized == "hands":
        return "hand"
    if normalized == "handoffs":
        return "handoff"
    if normalized == "recall":
        return "recall"
    return normalized
