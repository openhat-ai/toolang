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

    return statement_description(statement)


def statement_description(statement: FlowStmt) -> str:
    """Describe a statement's operation independently of its authored doc."""

    if isinstance(statement, LetStmt):
        return f"Set value to {statement.binding}"
    if isinstance(statement, RunStmt):
        action = f"Run {statement.runnable}"
    elif isinstance(statement, SeekStmt):
        action = f"Ask agent {statement.name} to run {statement.runnable}"
    elif isinstance(statement, AskStmt):
        action = (
            f"Ask {statement.name} for input"
            if statement.name
            else "Ask for human input"
        )
    elif isinstance(statement, ScatterStmt):
        action = _scatter_header(statement.runnable, statement.count)
    elif isinstance(statement, StormStmt):
        action = (
            f"Storm into {count(statement.count, 'item')} "
            f"with {statement.runnable} independently"
        )
    elif isinstance(statement, GatherStmt):
        action = f"Gather all items into one with {statement.runnable}"
    elif isinstance(statement, SettleStmt):
        action = f"Settle all items into one with {statement.runnable} sequentially"
    elif isinstance(statement, MapStmt):
        action = f"Map each item with {statement.runnable}"
    elif isinstance(statement, KeepStmt | DropStmt):
        verb = "Keep" if isinstance(statement, KeepStmt) else "Drop"
        if statement.position is not None and statement.count is not None:
            quantity = "item" if statement.count == 1 else f"{statement.count} items"
            action = f"{verb} the {statement.position} {quantity}"
        else:
            action = f"{verb} items where {statement.runnable} is true"
    elif isinstance(statement, SortStmt):
        action = f"Sort items by {statement.runnable} in {statement.order} order"
    elif isinstance(statement, RepeatStmt):
        if statement.count is not None and statement.runnable is not None:
            return (
                f"Repeat up to {count(statement.count, 'time')}, "
                f"until {statement.runnable} is true"
            )
        if statement.count is not None:
            return f"Repeat {count(statement.count, 'time')}"
        return f"Repeat until {statement.runnable} is true"
    else:
        raise TypeError(f"unsupported flow statement: {type(statement).__name__}")

    lanes = getattr(statement, "lanes", None)
    if lanes == 1:
        action += ", one at a time"
    elif isinstance(lanes, int):
        action += f", up to {lanes} at once"
    if statement.binding == "_":
        return action
    if statement.binding is None:
        return f"{action}, discard result"
    return f"{action}, save result to {statement.binding}"


def until_header(statement: RepeatStmt) -> str:
    """Return the Repeat until boundary label without exposing generated names."""

    runnable = statement.runnable or ""
    return "Check whether to stop" if _generated(runnable) else runnable


def _generated(value: str) -> bool:
    return not value or value.startswith("<agic:")


def _scatter_header(runnable: str, item_count: int | None) -> str:
    quantity = count(item_count, "item") if item_count is not None else "items"
    return f"Scatter into {quantity} with {runnable}"
