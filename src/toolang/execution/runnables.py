"""Effective runnable declarations shared by binding and execution."""

from __future__ import annotations

from toolang.lang.ast import AgicDecl, Parameter, Program, Span

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
