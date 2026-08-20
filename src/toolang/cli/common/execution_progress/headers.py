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
    RankStmt,
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
        action = _named_or_inline(
            statement.runnable,
            named="Run {name}",
            inline="Run the inline task",
        )
    elif isinstance(statement, SeekStmt):
        action = _named_or_inline(
            statement.runnable,
            named=f"Ask {statement.name} to run {{name}}",
            inline=f"Ask {statement.name} for help",
        )
    elif isinstance(statement, AskStmt):
        action = (
            f"Ask {statement.name} for input"
            if statement.name
            else "Ask for human input"
        )
    elif isinstance(statement, ScatterStmt):
        action = _named_or_inline(
            statement.runnable,
            named=f"Expand into {count(statement.count, 'item')} with {{name}}",
            inline=f"Expand into {count(statement.count, 'item')}",
        )
    elif isinstance(statement, StormStmt):
        action = _named_or_inline(
            statement.runnable,
            named=f"Run {{name}} {count(statement.count, 'time')}",
            inline=f"Generate {count(statement.count, 'item')}",
        )
    elif isinstance(statement, GatherStmt):
        action = _named_or_inline(
            statement.runnable,
            named="Combine the items with {name}",
            inline="Combine the items",
        )
    elif isinstance(statement, SettleStmt):
        action = _named_or_inline(
            statement.runnable,
            named="Reduce the items with {name}",
            inline="Reduce the items",
        )
    elif isinstance(statement, MapStmt):
        action = _named_or_inline(
            statement.runnable,
            named="Run {name} for each item",
            inline="Process each item",
        )
    elif isinstance(statement, KeepStmt | DropStmt):
        verb = "Keep" if isinstance(statement, KeepStmt) else "Drop"
        if statement.position is not None and statement.count is not None:
            quantity = "item" if statement.count == 1 else f"{statement.count} items"
            action = f"{verb} the {statement.position} {quantity}"
        else:
            action = _named_or_inline(
                statement.runnable or "",
                named=f"{verb} items selected by {{name}}",
                inline=f"{verb} selected items",
            )
    elif isinstance(statement, RankStmt):
        action = _named_or_inline(
            statement.runnable,
            named="Rank items with {name}",
            inline="Rank the items",
        )
        if statement.selection is not None and statement.limit is not None:
            action += (
                f" and keep the {statement.selection} {count(statement.limit, 'item')}"
            )
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
    """Return the Repeat until boundary label without exposing generated names."""

    runnable = statement.runnable or ""
    return "Check whether to stop" if _generated(runnable) else runnable


def _named_or_inline(value: str, *, named: str, inline: str) -> str:
    return inline if _generated(value) else named.format(name=value)


def _generated(value: str) -> bool:
    return not value or value.startswith("<agic:")


def _words(*values: str | None) -> str:
    return " ".join(value for value in values if value)
