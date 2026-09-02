"""Semantic validation for lowered Toolang programs."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

from toolang.common.errors import ToolangError

from . import ast
from .errors import ToolangValidationError
from .runnable_query import RUNNABLE_SCHEMA
from .types import validate_struct_type

_CAP_SOURCE_FIELDS: dict[ast.CapKind, frozenset[str]] = {
    "psyche": frozenset(),
    "skill": frozenset({"description"}),
    "service": frozenset(
        {"description", "transport", "protocol", "target", "headers", "env"}
    ),
    "prompt": frozenset(),
}
_CAP_REQUIRED_FIELDS: dict[ast.CapKind, frozenset[str]] = {
    "psyche": frozenset(),
    "skill": frozenset({"description"}),
    "service": frozenset({"description", "transport", "target"}),
    "prompt": frozenset(),
}
_CAP_BODY_REQUIRED = frozenset({"psyche", "skill", "prompt"})
_SERVICE_FIELDS = frozenset({"description", "transport", "target", "headers", "env"})
_RESERVED_RUNTIME_NAMES = frozenset({"far", "near", "line"})
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_PARAM_NAME_RE = re.compile(r"^[A-Za-z_][\w-]*$")


def _validate(program: ast.Program) -> None:
    """Validate one complete semantic AST."""

    _validate_caps(program.caps)
    _validate_structs(program.structs)
    contexts = _namespace(program.contexts, label="context")
    instructs = _namespace(program.instructs, label="instruct")
    runnables = _runnable_namespace(program)

    for agic in program.agics:
        _validate_parameters(agic.input, agic.params, owner=f"Agic {agic.name!r}")
        _validate_directives(
            agic.directives,
            owner=f"Agic {agic.name!r}",
            allow_routes=True,
            allow_recall=True,
        )
        _validate_prompt_ref(agic.context, contexts, target="context", owner=agic.name)
        _validate_prompt_ref(
            agic.instruct, instructs, target="instruct", owner=agic.name
        )

    for flow in program.flows:
        _validate_parameters(flow.input, flow.params, owner=f"Flow {flow.name!r}")
        _validate_directives(
            flow.directives,
            owner=f"Flow {flow.name!r}",
            allow_routes=False,
            allow_recall=False,
        )
        _validate_stmts(flow.stmts, runnables=runnables)


def _validate_cap_source(
    kind: ast.CapKind,
    name: str,
    body: str,
    properties: tuple[tuple[str, str, int], ...],
    *,
    line_number: int,
) -> dict[str, str]:
    """Validate ordered source properties before constructing immutable cap metadata."""

    meta: dict[str, str] = {}
    property_lines: dict[str, int] = {}
    for property_name, raw_value, property_line in properties:
        _validate_cap_property_name(kind, name, property_name, line=property_line)
        if first_line := property_lines.get(property_name):
            raise ToolangValidationError(
                f"{kind.capitalize()} cap {name!r} property {property_name!r} "
                f"at line {property_line} duplicates line {first_line}."
            )
        value = raw_value.strip()
        if not value:
            _raise_empty_cap_property(kind, name, property_name, line=property_line)
        meta[property_name] = value
        property_lines[property_name] = property_line

    if "transport" in meta and "protocol" in meta:
        raise ToolangValidationError(
            f"Service cap {name!r} properties 'transport' at line "
            f"{property_lines['transport']} and 'protocol' at line "
            f"{property_lines['protocol']} are mutually exclusive."
        )
    if protocol := meta.pop("protocol", None):
        meta["transport"] = protocol
        property_lines["transport"] = property_lines.pop("protocol")

    _validate_cap_contract(
        kind,
        name,
        body,
        meta,
        line_number=line_number,
        property_lines=property_lines,
    )
    return meta


def _validate_cap_property_name(
    kind: ast.CapKind, name: str, property_name: str, *, line: int
) -> None:
    allowed = _CAP_SOURCE_FIELDS[kind]
    if property_name in allowed:
        return
    if allowed:
        suffix = f"allowed properties are: {', '.join(sorted(allowed))}"
    else:
        suffix = f"{kind} caps allow no properties"
    raise ToolangValidationError(
        f"{kind.capitalize()} cap {name!r} property {property_name!r} "
        f"at line {line} is unsupported; {suffix}."
    )


def _raise_empty_cap_property(
    kind: ast.CapKind, name: str, property_name: str, *, line: int
) -> None:
    _validate_cap_property_name(kind, name, property_name, line=line)
    raise ToolangValidationError(
        f"{kind.capitalize()} cap {name!r} property {property_name!r} "
        f"at line {line} must be nonempty."
    )


def _validate_caps(caps: tuple[ast.CapDecl, ...]) -> None:
    seen: set[tuple[ast.CapKind, str]] = set()
    for cap in caps:
        key = (cap.kind, cap.name)
        if key in seen:
            raise ToolangValidationError(f"Duplicate {cap.kind} name {cap.name!r}.")
        seen.add(key)
        _validate_cap_contract(
            cap.kind,
            cap.name,
            cap.body,
            cap.meta,
            line_number=cap.span.line,
            property_lines={},
        )
        if cap.kind == "prompt":
            _unique(
                (item.name for item in cap.params),
                label=f"parameter in prompt {cap.name!r}",
            )
            for param in cap.params:
                if param.name == "_":
                    raise ToolangValidationError(
                        f"Prompt parameter '_' is reserved for primary input at line {param.span.line}."
                    )
                if _PARAM_NAME_RE.fullmatch(param.name) is None:
                    raise ToolangValidationError(
                        f"Invalid prompt parameter {param.name!r} at line {param.span.line}."
                    )
                if param.optional or param.type_name != "Text":
                    raise ToolangValidationError(
                        f"Prompt parameter {param.name!r} at line {param.span.line} "
                        "must be required Text."
                    )


def _validate_cap_contract(
    kind: ast.CapKind,
    name: str,
    body: str,
    meta: Mapping[str, Any],
    *,
    line_number: int,
    property_lines: Mapping[str, int],
) -> None:
    allowed = _SERVICE_FIELDS if kind == "service" else _CAP_SOURCE_FIELDS[kind]
    if unknown := sorted(set(meta) - allowed):
        property_name = unknown[0]
        property_line = property_lines.get(property_name, line_number)
        if allowed:
            suffix = f"allowed properties are: {', '.join(sorted(allowed))}"
        else:
            suffix = f"{kind} caps allow no properties"
        raise ToolangValidationError(
            f"{kind.capitalize()} cap {name!r} property {property_name!r} "
            f"at line {property_line} is unsupported; {suffix}."
        )

    for property_name, value in meta.items():
        property_line = property_lines.get(property_name, line_number)
        if not isinstance(value, str) or not value.strip():
            raise ToolangValidationError(
                f"{kind.capitalize()} cap {name!r} property {property_name!r} "
                f"at line {property_line} must be nonempty inline text."
            )

    for property_name in sorted(_CAP_REQUIRED_FIELDS[kind] - set(meta)):
        raise ToolangValidationError(
            f"{kind.capitalize()} cap {name!r} is missing required property "
            f"{property_name!r} at line {line_number}."
        )

    if kind in _CAP_BODY_REQUIRED and not body.strip():
        raise ToolangValidationError(
            f"{kind.capitalize()} cap {name!r} requires a nonempty body "
            f"at line {line_number}."
        )

    if kind != "service":
        return
    transport = meta.get("transport")
    if transport not in {"http", "stdio"}:
        raise ToolangValidationError(
            f"Service cap {name!r} property 'transport' at line "
            f"{property_lines.get('transport', line_number)} must be 'http' or 'stdio'."
        )
    if "headers" in meta and transport != "http":
        raise ToolangValidationError(
            f"Service cap {name!r} property 'headers' at line "
            f"{property_lines.get('headers', line_number)} is valid only for HTTP."
        )
    if env := meta.get("env"):
        _validate_service_env(
            name,
            env,
            line_number=property_lines.get("env", line_number),
        )


def _validate_service_env(name: str, raw: object, *, line_number: int) -> None:
    if not isinstance(raw, str):
        raise ToolangValidationError(
            f"Service cap {name!r} property 'env' at line {line_number} "
            "must list environment variable names."
        )
    values = [item.strip() for item in raw.split(",")]
    if any(_ENV_NAME_RE.fullmatch(item) is None for item in values):
        raise ToolangValidationError(
            f"Service cap {name!r} property 'env' at line {line_number} "
            "must contain comma-separated environment names."
        )
    if len(values) != len(set(values)):
        raise ToolangValidationError(
            f"Service cap {name!r} property 'env' at line {line_number} "
            "must not contain duplicate environment names."
        )


def _runnable_namespace(program: ast.Program) -> dict[str, ast.AgicDecl | ast.FlowDecl]:
    values: dict[str, ast.AgicDecl | ast.FlowDecl] = {}
    for item in (*program.agics, *program.flows):
        if item.name in values:
            raise ToolangValidationError(f"Duplicate runnable name {item.name!r}.")
        values[item.name] = item
    return values


def _namespace(
    items: Iterable[ast.ContextDecl | ast.InstructDecl], *, label: str
) -> dict[str, object]:
    values: dict[str, object] = {}
    for item in items:
        if item.name in values:
            raise ToolangValidationError(f"Duplicate {label} name {item.name!r}.")
        values[item.name] = item
    return values


def _unique(values: Iterable[str], *, label: str) -> None:
    seen: set[str] = set()
    for value in values:
        if value in seen:
            raise ToolangValidationError(f"Duplicate {label} name {value!r}.")
        seen.add(value)


def _validate_structs(structs: tuple[ast.StructDecl, ...]) -> None:
    _unique((item.name for item in structs), label="struct")
    for item in structs:
        try:
            validate_struct_type(item.name)
        except ValueError as exc:
            raise ToolangValidationError(
                f"Struct name {item.name!r} conflicts with a built-in type."
            ) from exc


def _validate_parameters(
    input_param: ast.Parameter | None,
    params: tuple[ast.Parameter, ...],
    *,
    owner: str,
) -> None:
    if input_param is not None and input_param.optional:
        raise ToolangValidationError(f"{owner} primary input '_' must not be optional.")
    seen = {input_param.name} if input_param is not None else set()
    if "runtime" in seen:
        raise ToolangValidationError(
            f"{owner} must not use reserved parameter name 'runtime'."
        )
    if reserved := seen & _RESERVED_RUNTIME_NAMES:
        name = next(iter(reserved))
        raise ToolangValidationError(
            f"{owner} must not use reserved runtime parameter name {name!r}."
        )
    for param in params:
        if param.name == "_":
            raise ToolangValidationError(
                f"{owner} primary input '_' must be the first parameter."
            )
        if param.name == "runtime":
            raise ToolangValidationError(
                f"{owner} must not use reserved parameter name 'runtime'."
            )
        if param.name in _RESERVED_RUNTIME_NAMES:
            raise ToolangValidationError(
                f"{owner} must not use reserved runtime parameter name {param.name!r}."
            )
        if param.name in seen:
            raise ToolangValidationError(
                f"Duplicate parameter {param.name!r} in {owner}."
            )
        seen.add(param.name)


def _validate_directives(
    directives: tuple[ast.Directive, ...],
    *,
    owner: str,
    allow_routes: bool,
    allow_recall: bool,
) -> None:
    models = [item for item in directives if item.name == "models"]
    for directive in models:
        if not directive.values:
            raise ToolangValidationError(
                f"{owner} must declare at least one model query."
            )
    for name in ("hands", "handoffs"):
        routes = [item for item in directives if item.name == name]
        if routes and not allow_routes:
            raise ToolangValidationError(
                f"{owner} must not declare the {name} routing directive."
            )
        if len(routes) > 1:
            raise ToolangValidationError(
                f"{owner} may declare at most one {name} directive."
            )
        if not routes:
            continue
        directive = routes[0]
        if directive.operator != "=":
            raise ToolangValidationError(
                f"{owner} must use '=' for its {name} directive."
            )
        if not directive.values:
            raise ToolangValidationError(
                f"{owner} must declare at least one public runnable in its {name} directive."
            )
        for value in directive.values:
            try:
                RUNNABLE_SCHEMA.parse(value)
            except ToolangError as exc:
                raise ToolangValidationError(
                    f"{owner} declares invalid runnable query {value!r} in its {name} directive."
                ) from exc

    for directive in (item for item in directives if item.name == "tools"):
        internal = [
            value
            for value in directive.values
            if value == "_too"
            or value.startswith("_too/")
            or value.startswith("_too__")
        ]
        if internal:
            raise ToolangValidationError(
                f"{owner} must authorize runnable routes with hands or handoffs, "
                f"not tools: {', '.join(internal)}"
            )

    recalls = [item for item in directives if item.name == "recall"]
    if recalls and not allow_recall:
        raise ToolangValidationError(f"{owner} must not declare the recall directive.")
    if len(recalls) > 1:
        raise ToolangValidationError(
            f"{owner} may declare at most one recall directive."
        )
    if not recalls:
        return
    recall = recalls[0]
    if recall.operator != "=":
        raise ToolangValidationError(f"{owner} must use '=' for its recall directive.")
    values = set(recall.values)
    if values in (
        {"none"},
        {"auto"},
        {"far"},
        {"near"},
        {"far", "near"},
    ):
        return
    if not values:
        raise ToolangValidationError(
            f"{owner} must declare at least one recall source."
        )
    raise ToolangValidationError(
        f"{owner} has unsupported recall directive values: {', '.join(recall.values)}."
    )


def _validate_prompt_ref(
    ref: str | None, namespace: dict[str, object], *, target: str, owner: str
) -> None:
    if ref is None or ref in {"default", "none"}:
        return
    if ref not in namespace:
        raise ToolangValidationError(
            f"Agic {owner!r} references unknown {target} {ref!r}."
        )


def _validate_stmts(
    stmts: tuple[ast.FlowStmt, ...],
    *,
    runnables: dict[str, ast.AgicDecl | ast.FlowDecl],
) -> None:
    for stmt in stmts:
        _validate_binding(stmt)
        if isinstance(stmt, ast.SeekStmt):
            if stmt.runnable.startswith("<"):
                _require_runnable(stmt.runnable, runnables, stmt=stmt)
            continue
        if isinstance(stmt, ast.AskStmt | ast.LetStmt):
            if isinstance(stmt, ast.LetStmt) and stmt.binding in {None, "_"}:
                raise ToolangValidationError(
                    f"Let statement at line {stmt.span.line} requires a named binding."
                )
            continue
        if isinstance(stmt, ast.KeepStmt | ast.DropStmt):
            positional = stmt.position is not None or stmt.count is not None
            filtered = stmt.runnable is not None
            if positional == filtered:
                raise ToolangValidationError(
                    f"{stmt.kind.capitalize()} at line {stmt.span.line} requires position or predicate."
                )
            if positional:
                if (
                    stmt.position is None
                    or stmt.count is None
                    or stmt.lanes is not None
                ):
                    raise ToolangValidationError(
                        f"Invalid positional {stmt.kind} at line {stmt.span.line}."
                    )
                _non_negative(stmt.count, field="count", line=stmt.span.line)
            else:
                _require_runnable(stmt.runnable or "", runnables, stmt=stmt)
                _positive_optional(stmt.lanes, field="par", line=stmt.span.line)
            continue
        if isinstance(stmt, ast.RankStmt):
            _require_runnable(stmt.runnable, runnables, stmt=stmt)
            if (stmt.selection is None) != (stmt.limit is None):
                raise ToolangValidationError(
                    f"Rank at line {stmt.span.line} has an incomplete limit."
                )
            if stmt.limit is not None:
                _non_negative(stmt.limit, field="count", line=stmt.span.line)
            _positive_optional(stmt.lanes, field="par", line=stmt.span.line)
            continue
        if isinstance(stmt, ast.RepeatStmt):
            if stmt.count is None and stmt.runnable is None:
                raise ToolangValidationError(
                    f"Repeat at line {stmt.span.line} requires count or until."
                )
            if stmt.count is not None:
                _non_negative(stmt.count, field="count", line=stmt.span.line)
            if stmt.runnable is not None:
                _require_runnable(stmt.runnable, runnables, stmt=stmt)
            _validate_stmts(stmt.stmts, runnables=runnables)
            continue

        runnable = _stmt_runnable(stmt)
        _require_runnable(runnable, runnables, stmt=stmt)
        if isinstance(stmt, ast.ScatterStmt | ast.StormStmt):
            _non_negative(stmt.count, field="count", line=stmt.span.line)
        if isinstance(stmt, ast.StormStmt | ast.MapStmt):
            _positive_optional(stmt.lanes, field="par", line=stmt.span.line)


def _validate_binding(stmt: ast.FlowStmt) -> None:
    binding = stmt.binding
    if binding in _RESERVED_RUNTIME_NAMES:
        raise ToolangValidationError(
            f"Flow binding {binding!r} at line {stmt.span.line} is reserved "
            "for a runtime local."
        )
    if (
        binding is not None
        and binding != "_"
        and not re.fullmatch(r"[a-z][a-z0-9_]*", binding)
    ):
        raise ToolangValidationError(
            f"Invalid binding {binding!r} at line {stmt.span.line}."
        )


def _stmt_runnable(stmt: ast.FlowStmt) -> str:
    if isinstance(
        stmt,
        ast.RunStmt
        | ast.ScatterStmt
        | ast.StormStmt
        | ast.GatherStmt
        | ast.SettleStmt
        | ast.MapStmt,
    ):
        return stmt.runnable
    raise RuntimeError(f"Statement {stmt.kind!r} has no runnable field.")


def _require_runnable(
    name: str,
    runnables: dict[str, ast.AgicDecl | ast.FlowDecl],
    *,
    stmt: ast.FlowStmt,
) -> None:
    if name not in runnables:
        raise ToolangValidationError(
            f"{stmt.kind.capitalize()} at line {stmt.span.line} references unknown runnable {name!r}."
        )


def _non_negative(value: int, *, field: str, line: int) -> None:
    if value < 0:
        raise ToolangValidationError(f"{field} at line {line} must not be negative.")


def _positive_optional(value: int | None, *, field: str, line: int) -> None:
    if value is not None and value <= 0:
        raise ToolangValidationError(f"{field} at line {line} must be positive.")
