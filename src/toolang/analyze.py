from __future__ import annotations

from toolang.ast import Program
from toolang.errors import ToolangError


def analyze_program(program: Program) -> None:
    _validate_declarations(program)
    _validate_thunks(program)


def _validate_declarations(program: Program) -> None:
    seen: set[tuple[str, str]] = set()
    for declaration in program.declarations:
        key = (declaration.kind, declaration.name)
        if key in seen:
            raise ToolangError(
                f"Duplicate {declaration.kind} declaration {declaration.name!r} at line {declaration.span.line}."
            )
        seen.add(key)


def _validate_thunks(program: Program) -> None:
    if not program.thunks:
        raise ToolangError("No thunk found in source.")

    seen_named: set[str] = set()
    default_seen = False
    structs = {declaration.name for declaration in program.declarations_by_kind("struct")}

    for thunk in program.thunks:
        if thunk.name is None:
            if default_seen:
                raise ToolangError(f"Duplicate default thunk at line {thunk.span.line}.")
            default_seen = True
        else:
            if thunk.name in seen_named:
                raise ToolangError(f"Duplicate thunk {thunk.name!r} at line {thunk.span.line}.")
            seen_named.add(thunk.name)

        if thunk.output and thunk.output not in structs:
            raise ToolangError(
                f"Thunk {thunk.name or '<default>'} refers to unknown output struct {thunk.output!r} at line {thunk.span.line}."
            )
