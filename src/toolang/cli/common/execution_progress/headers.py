"""Flow Step and loop boundary header projection."""

from __future__ import annotations

from toolang.execution.events import FlowStepInfo
from toolang.lang.types import is_generated_ref
from toolang.lang.description import statement_description

from .formatting import one_line


def statement_header(statement: FlowStepInfo) -> str:
    """Return one concise presentation header from a typed Flow statement."""

    if statement.doc and (doc := one_line(statement.doc.strip())):
        return doc

    return statement_description(statement)


def until_header(statement: FlowStepInfo) -> str:
    """Return the Repeat until boundary label without exposing generated names."""

    runnable = statement.runnable or ""
    return (
        "Check whether to break"
        if _generated(runnable)
        else f"Run {runnable} to check whether to break"
    )


def _generated(value: str) -> bool:
    return not value or is_generated_ref(value)
