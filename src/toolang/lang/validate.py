"""Semantic validation for lowered Toolang programs."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

from . import ast
from .diagnostics import ToolangValidationError

SERVICE_FIELDS = frozenset(
    {"description", "transport", "protocol", "target", "headers", "env"}
)
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
PARAM_NAME_RE = re.compile(r"^[A-Za-z_][\w-]*$")


def validate(program: ast.Program) -> None:
    """Validate one complete semantic AST."""

    _validate_caps(program.caps)
    _unique((item.name for item in program.structs), label="struct")
    contexts = _namespace(program.contexts, label="context")
    instructs = _namespace(program.instructs, label="instruct")
    runnables = _runnable_namespace(program)

    for agic in program.agics:
        _validate_parameters(agic.input, agic.params, owner=f"Agic {agic.name!r}")
        _validate_directives(agic.directives, owner=f"Agic {agic.name!r}")
        _validate_prompt_ref(agic.context, contexts, target="context", owner=agic.name)
        _validate_prompt_ref(
            agic.instruct, instructs, target="instruct", owner=agic.name
        )

    for flow in program.flows:
        _validate_parameters(flow.input, flow.params, owner=f"Flow {flow.name!r}")
        _validate_directives(flow.directives, owner=f"Flow {flow.name!r}")
        _validate_stmts(flow.stmts, runnables=runnables)


def validate_service_meta(
    meta: Mapping[str, Any],
    *,
    line_number: int,
    require_description: bool = False,
) -> None:
    _require_exact_fields(meta, SERVICE_FIELDS, kind="service", line=line_number)
    description = meta.get("description")
    if require_description and (not isinstance(description, str) or not description):
        raise ToolangValidationError(
            f"Service cap at line {line_number} is missing description."
        )
    if description is not None and not isinstance(description, str):
        raise ToolangValidationError(
            f"Service cap at line {line_number} must define description as a string."
        )
    transport = meta.get("transport") or meta.get("protocol")
    if not isinstance(transport, str) or not transport:
        raise ToolangValidationError(
            f"Service cap at line {line_number} is missing protocol."
        )
    if transport not in {"http", "stdio"}:
        raise ToolangValidationError(
            f"Service cap at line {line_number} uses unsupported transport {transport!r}."
        )
    target = meta.get("target")
    if not isinstance(target, str) or not target:
        raise ToolangValidationError(
            f"Service cap at line {line_number} is missing target."
        )
    headers = meta.get("headers")
    if (
        headers is not None
        and not isinstance(headers, str)
        and not _is_string_map(headers)
    ):
        raise ToolangValidationError(
            f"Service cap at line {line_number} must define headers as a string map."
        )
    env = meta.get("env")
    if env is not None and not _is_env_names(env):
        raise ToolangValidationError(
            f"Service cap at line {line_number} must list environment variable names."
        )


def _validate_caps(caps: tuple[ast.CapDecl, ...]) -> None:
    seen: set[tuple[ast.CapKind, str]] = set()
    for cap in caps:
        key = (cap.kind, cap.name)
        if key in seen:
            raise ToolangValidationError(f"Duplicate {cap.kind} name {cap.name!r}.")
        seen.add(key)
        if cap.kind == "service":
            validate_service_meta(
                cap.meta, line_number=cap.span.line, require_description=True
            )
        elif cap.kind == "prompt":
            _require_exact_fields(
                cap.meta, frozenset({"params"}), kind="prompt", line=cap.span.line
            )
            _unique(
                (item.name for item in cap.params),
                label=f"parameter in prompt {cap.name!r}",
            )
            for param in cap.params:
                if PARAM_NAME_RE.fullmatch(param.name) is None:
                    raise ToolangValidationError(
                        f"Invalid prompt parameter {param.name!r} at line {param.span.line}."
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


def _validate_parameters(
    input_param: ast.Parameter | None,
    params: tuple[ast.Parameter, ...],
    *,
    owner: str,
) -> None:
    seen = {input_param.name} if input_param is not None else set()
    if "runtime" in seen:
        raise ToolangValidationError(
            f"{owner} must not use reserved parameter name 'runtime'."
        )
    for param in params:
        if param.name == "runtime":
            raise ToolangValidationError(
                f"{owner} must not use reserved parameter name 'runtime'."
            )
        if param.name in seen:
            raise ToolangValidationError(
                f"Duplicate parameter {param.name!r} in {owner}."
            )
        seen.add(param.name)


def _validate_directives(directives: tuple[ast.Directive, ...], *, owner: str) -> None:
    models = [item for item in directives if item.name == "models"]
    if len(models) > 1:
        raise ToolangValidationError(
            f"{owner} may declare at most one models directive."
        )
    if models:
        directive = models[0]
        if directive.operator != "=":
            raise ToolangValidationError(
                f"{owner} must use '=' for its models directive."
            )
        if not directive.values:
            raise ToolangValidationError(
                f"{owner} must declare at least one model selector."
            )
        if routed := [selector for selector in directive.values if "@" in selector]:
            raise ToolangValidationError(
                f"{owner} must declare route-neutral model refs, not routed selectors: {', '.join(routed)}"
            )

    recalls = [item for item in directives if item.name == "recall"]
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
        {"default"},
        {"history"},
        {"memory"},
        {"history", "memory"},
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
            filtered = stmt.predicate is not None
            if positional == filtered:
                raise ToolangValidationError(
                    f"{stmt.kind.capitalize()} at line {stmt.span.line} requires position or predicate."
                )
            if positional:
                if stmt.position is None or stmt.count is None or stmt.par is not None:
                    raise ToolangValidationError(
                        f"Invalid positional {stmt.kind} at line {stmt.span.line}."
                    )
                _non_negative(stmt.count, field="count", line=stmt.span.line)
            else:
                _require_runnable(stmt.predicate or "", runnables, stmt=stmt)
                _positive_optional(stmt.par, field="par", line=stmt.span.line)
            continue
        if isinstance(stmt, ast.RankStmt):
            _require_runnable(stmt.scorer, runnables, stmt=stmt)
            if (stmt.limit is None) != (stmt.count is None):
                raise ToolangValidationError(
                    f"Rank at line {stmt.span.line} has an incomplete limit."
                )
            if stmt.count is not None:
                _non_negative(stmt.count, field="count", line=stmt.span.line)
            _positive_optional(stmt.par, field="par", line=stmt.span.line)
            continue
        if isinstance(stmt, ast.RepeatStmt):
            if stmt.count is None and stmt.until is None:
                raise ToolangValidationError(
                    f"Repeat at line {stmt.span.line} requires count or until."
                )
            if stmt.count is not None:
                _non_negative(stmt.count, field="count", line=stmt.span.line)
            if stmt.until is not None:
                _require_runnable(stmt.until, runnables, stmt=stmt)
            _validate_stmts(stmt.stmts, runnables=runnables)
            continue

        runnable = _stmt_runnable(stmt)
        _require_runnable(runnable, runnables, stmt=stmt)
        if isinstance(stmt, ast.ScatterStmt | ast.StormStmt):
            _non_negative(stmt.count, field="count", line=stmt.span.line)
        if isinstance(stmt, ast.StormStmt | ast.MapStmt):
            _positive_optional(stmt.par, field="par", line=stmt.span.line)


def _validate_binding(stmt: ast.FlowStmt) -> None:
    binding = stmt.binding
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


def _require_exact_fields(
    meta: Mapping[str, Any], allowed: frozenset[str], *, kind: str, line: int
) -> None:
    if unknown := sorted(set(meta) - allowed):
        raise ToolangValidationError(
            f"{kind.capitalize()} at line {line} has unsupported field(s): {', '.join(unknown)}."
        )


def _is_string_map(value: object) -> bool:
    return isinstance(value, Mapping) and all(
        isinstance(key, str) and isinstance(item, str) for key, item in value.items()
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
    return bool(items) and all(
        ENV_NAME_RE.fullmatch(item) is not None for item in items
    )
