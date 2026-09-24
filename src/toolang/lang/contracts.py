"""Local runnable contracts required by flow operations."""

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from .ast import AgicDecl, FlowDecl, StructDecl
from .errors import ToolangValidationError


@dataclass(frozen=True, slots=True)
class OutputContract:
    """One output type and its referenced struct definitions at a call boundary."""

    type_name: str
    definitions: Mapping[str, tuple[tuple[str, str, bool], ...]]

    @classmethod
    def resolve(
        cls, type_name: str, *, structs: Mapping[str, StructDecl]
    ) -> "OutputContract":
        definitions: dict[str, tuple[tuple[str, str, bool], ...]] = {}
        pending = [type_name]
        while pending:
            name = pending.pop().partition("[")[0]
            declaration = structs.get(name)
            if declaration is None or name in definitions:
                continue
            definitions[name] = tuple(
                sorted(
                    (field.name, field.type_name, field.optional)
                    for field in declaration.fields
                )
            )
            pending.extend(field.type_name for field in declaration.fields)
        return cls(type_name, MappingProxyType(definitions))

    def validate(
        self, type_name: str, *, structs: Mapping[str, StructDecl], name: str
    ) -> None:
        if type_name != self.type_name:
            raise ToolangValidationError(
                f"{name!r} requires {self.type_name} output, got {type_name}"
            )
        if self != self.resolve(type_name, structs=structs):
            raise ToolangValidationError(
                f"{name!r} requires {self.type_name} output with unchanged struct definitions"
            )


def validate_operation_contract(
    operation: str,
    runnable: AgicDecl | FlowDecl,
    *,
    name: str,
    line: int,
) -> None:
    """Check an already resolved signature without inferring through callees."""

    label = f"{operation.capitalize()} at line {line}"
    if operation in {"map", "keep", "drop", "sort", "gather", "settle"}:
        if runnable.input is None:
            raise ToolangValidationError(
                f"{label} requires primary input '_' in {name!r}."
            )
    output = runnable.output
    expected = {
        "keep": "Boolean",
        "drop": "Boolean",
        "sort": "Number",
        "repeat": "Boolean",
    }.get(operation)
    if expected is not None and output != expected:
        raise ToolangValidationError(
            f"{label} requires {expected} output from {name!r}, got {output}."
        )
    if operation == "scatter" and (output is None or not output.endswith("[]")):
        raise ToolangValidationError(
            f"{label} requires array output from {name!r}, got {output}."
        )
