"""Local runnable contracts required by flow operations."""

from .ast import AgicDecl, FlowDecl
from .errors import ToolangValidationError


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
