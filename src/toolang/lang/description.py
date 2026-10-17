"""Natural-language descriptions of authored Flow statements."""

from __future__ import annotations

from toolang.lang.types import display_runnable_ref
from typing import Protocol


class DescribedStatement(Protocol):
    """Common operation facts; optional variant fields are read by kind."""

    @property
    def kind(self) -> str: ...

    @property
    def binding(self) -> str | None: ...


def _runnable_label(value: str | None) -> str:
    return display_runnable_ref(value, surface="progress") if value else ""


def statement_description(statement: DescribedStatement) -> str:
    """Describe a statement's operation independently of its authored doc."""

    runnable: str | None = getattr(statement, "runnable", None)
    count: int | None = getattr(statement, "count", None)
    name: str = getattr(statement, "name", "")
    handle: str = getattr(statement, "handle", "")
    asynchronous: bool = getattr(statement, "asynchronous", False)
    position: str | None = getattr(statement, "position", None)
    order: str = getattr(statement, "order", "ascending")

    if statement.kind == "exec":
        return f"Exec {_runnable_label(runnable)}"
    if statement.kind == "let":
        return f"Set value to {statement.binding}"
    if statement.kind == "await":
        return f"Await {handle}"
    if statement.kind == "run":
        action = f"{'Start' if asynchronous else 'Run'} {_runnable_label(runnable)}"
    elif statement.kind == "spawn":
        action = f"Spawn {_runnable_label(runnable)}"
    elif statement.kind == "seek":
        action = f"Ask agent {name} to run {_runnable_label(runnable)}"
    elif statement.kind == "ask":
        action = f"Ask {name} for input" if name else "Ask for human input"
    elif statement.kind == "generate":
        action = (
            f"Generate into {_count(count or 0, 'item')} "
            f"with {_runnable_label(runnable)} independently"
        )
    elif statement.kind == "reduce":
        action = (
            f"Reduce all items into one with {_runnable_label(runnable)} sequentially"
        )
    elif statement.kind == "map":
        action = f"Map each item with {_runnable_label(runnable)}"
    elif statement.kind in {"keep", "drop"}:
        verb = "Keep" if statement.kind == "keep" else "Drop"
        if position is not None and count is not None:
            quantity = "item" if count == 1 else f"{count} items"
            action = f"{verb} the {position} {quantity}"
        else:
            action = f"{verb} items where {_runnable_label(runnable)} is true"
    elif statement.kind == "sort":
        action = f"Sort items by {_runnable_label(runnable)} in {order} order"
    elif statement.kind == "repeat":
        if count is not None and runnable is not None:
            return (
                f"Repeat up to {_count(count, 'time')}, "
                f"until {_runnable_label(runnable)} is true"
            )
        if count is not None:
            return f"Repeat {_count(count, 'time')}"
        if runnable is None:
            return "Repeat indefinitely"
        return f"Repeat until {_runnable_label(runnable)} is true"
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


def _count(value: int, noun: str) -> str:
    return f"{value} {noun}{'' if value == 1 else 's'}"
