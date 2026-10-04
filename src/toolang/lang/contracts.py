"""Local runnable contracts required by flow operations."""

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

from toolang.common.template import template_root_names

from .ast import AgicDecl, FlowDecl, Parameter, StructDecl
from .errors import ToolangValidationError
from .types import is_generated_ref

FlowTransform = Literal["item", "list", "filter", "sort", "none"]


def operation_transform(operation: str) -> FlowTransform:
    """Return the flow result transform shared by execution and source checks."""
    if operation in {"repeat", "exec"}:
        return "none"
    if operation in {"scatter", "storm", "map"}:
        return "list"
    if operation in {"keep", "drop"}:
        return "filter"
    if operation == "sort":
        return "sort"
    return "item"


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


@dataclass(frozen=True, slots=True)
class RunnableContract:
    """Normalized public data contract, independent of implementation text."""

    kind: str
    primary: tuple[str, str, bool] | None
    parameters: tuple[tuple[str, str, bool], ...]
    output: str | None
    definitions: Mapping[str, tuple[tuple[str, str, bool], ...]]

    @classmethod
    def resolve(
        cls, runnable: AgicDecl | FlowDecl, *, structs: Mapping[str, StructDecl]
    ) -> "RunnableContract":
        def parameter(item: Parameter) -> tuple[str, str, bool]:
            return item.name, item.type_name or "Part[]", item.optional

        primary = parameter(runnable.input) if runnable.input is not None else None
        parameters = tuple(sorted(parameter(item) for item in runnable.params))
        output = (
            (runnable.output or "Part[]")
            if isinstance(runnable, AgicDecl)
            else runnable.output
        )
        definitions: dict[str, tuple[tuple[str, str, bool], ...]] = {}
        for name in (
            *((output,) if output is not None else ()),
            *((primary[1],) if primary is not None else ()),
            *(item[1] for item in parameters),
        ):
            definitions.update(
                OutputContract.resolve(name, structs=structs).definitions
            )
        return cls(
            runnable.kind, primary, parameters, output, MappingProxyType(definitions)
        )


def validate_operation_contract(
    operation: str,
    runnable: AgicDecl | FlowDecl,
    *,
    name: str,
    line: int,
) -> None:
    """Check an already resolved signature without inferring through callees."""

    label = operation.capitalize()
    if operation in {"map", "keep", "drop", "sort", "gather", "settle"}:
        if runnable.input is None and not (
            isinstance(runnable, AgicDecl)
            and is_generated_ref(name)
            and any("_" in template_root_names(m.content) for m in runnable.messages)
        ):
            raise ToolangValidationError(
                f"{label} requires primary input '_' in {name!r}", line=line
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
            f"{label} requires {expected} output from {name!r}, got {output}", line=line
        )
    if operation == "scatter" and (output is None or not output.endswith("[]")):
        raise ToolangValidationError(
            f"{label} requires array output from {name!r}, got {output}", line=line
        )
