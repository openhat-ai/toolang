"""Flow Step and loop boundary header projection."""

from __future__ import annotations

from toolang.lang.ast import (
    AskStmt,
    DropStmt,
    FlowStmt,
    GatherStmt,
    KeepStmt,
    LetStmt,
    MapStmt,
    SortStmt,
    RepeatStmt,
    RunStmt,
    ScatterStmt,
    SeekStmt,
    SettleStmt,
    StormStmt,
)

from .formatting import count, one_line


def statement_header(statement: FlowStmt) -> str:
    """Return one concise presentation header from a typed Flow statement."""

    if statement.doc and (doc := one_line(statement.doc.strip())):
        return doc

    if isinstance(statement, LetStmt):
        return _words("Set", statement.binding or "value")
    if isinstance(statement, RunStmt):
        action = f"Run {statement.runnable}"
    elif isinstance(statement, SeekStmt):
        action = f"Ask {statement.name} to run {statement.runnable}"
    elif isinstance(statement, AskStmt):
        action = (
            f"Ask {statement.name} for input"
            if statement.name
            else "Ask for human input"
        )
    elif isinstance(statement, ScatterStmt):
        action = (
            f"Expand into {count(statement.count, 'item')} with {statement.runnable}"
        )
    elif isinstance(statement, StormStmt):
        action = f"Run {statement.runnable} {count(statement.count, 'time')}"
    elif isinstance(statement, GatherStmt):
        action = f"Combine the items with {statement.runnable}"
    elif isinstance(statement, SettleStmt):
        action = f"Reduce the items with {statement.runnable}"
    elif isinstance(statement, MapStmt):
        action = f"Run {statement.runnable} for each item"
    elif isinstance(statement, KeepStmt | DropStmt):
        verb = "Keep" if isinstance(statement, KeepStmt) else "Drop"
        if statement.position is not None and statement.count is not None:
            quantity = "item" if statement.count == 1 else f"{statement.count} items"
            action = f"{verb} the {statement.position} {quantity}"
        else:
            action = f"{verb} items selected by {statement.runnable}"
    elif isinstance(statement, SortStmt):
        action = f"Sort items {statement.order} by {statement.runnable}"
    elif isinstance(statement, RepeatStmt):
        if statement.count is not None and statement.runnable is not None:
            return f"Repeat up to {count(statement.count, 'time')}"
        if statement.count is not None:
            return f"Repeat {count(statement.count, 'time')}"
        return "Repeat until complete"
    else:
        raise TypeError(f"unsupported flow statement: {type(statement).__name__}")

    lanes = getattr(statement, "lanes", None)
    if isinstance(lanes, int):
        action += f", up to {lanes} at once"
    if statement.binding == "_":
        return action
    if statement.binding is None:
        return f"{action} without saving the result"
    return f"{action} and save as {statement.binding}"


def until_header(statement: RepeatStmt) -> str:
    """Return the Repeat condition's runnable name as its boundary label."""

    return statement.runnable or "Check whether to stop"


def _words(*values: str | None) -> str:
    return " ".join(value for value in values if value)
