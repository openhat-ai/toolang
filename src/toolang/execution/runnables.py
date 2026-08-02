"""Runnable declaration lookup shared by binding and execution."""

from __future__ import annotations

from typing import TypeAlias

from toolang.base.errors import ToolangError
from toolang.lang.ast import AgicDecl, FlowDecl, Parameter, Program, Span

Runnable: TypeAlias = AgicDecl | FlowDecl

_RUNTIME_DEFAULT_AGIC = AgicDecl(
    name="default",
    input=Parameter(name="_", type_name="Part[]", span=Span(line=1)),
    span=Span(line=1),
)


def effective_agics(program: Program) -> tuple[AgicDecl, ...]:
    """Return authored agics plus the implicit runtime default when needed."""

    if program.find_agic("default") is not None:
        return program.agics
    return (*program.agics, _RUNTIME_DEFAULT_AGIC)


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
