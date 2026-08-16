"""Shared terminal-chat policy state operations."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import cast

from toolang.execution.policy import merge_commands
from toolang.execution.runnables import parse_runnable_ref
from toolang.execution.types import RunOverride

_OVERRIDES_KEY = "run_overrides"


def commands_from_selects(
    selects: Mapping[str, object],
) -> tuple[RunOverride, ...]:
    """Project canonical session commands from chat presentation state."""

    stored = selects.get(_OVERRIDES_KEY)
    if isinstance(stored, tuple) and all(
        isinstance(item, RunOverride) for item in stored
    ):
        return cast(tuple[RunOverride, ...], stored)

    commands: list[RunOverride] = []
    model = _text(selects.get("model"))
    if model is not None:
        commands.append(RunOverride("default", "model", model))
    runnable = _text(selects.get("runnable"))
    if runnable is not None:
        commands.append(RunOverride("default", "runnable", runnable))
        return tuple(commands)
    for kind in ("flow", "agic"):
        name = _text(selects.get(kind))
        if name is not None:
            commands.append(RunOverride("default", "runnable", f"{kind}:{name}"))
            break
    return tuple(commands)


def apply_session_commands(
    selects: Mapping[str, object],
    updates: Sequence[RunOverride],
) -> dict[str, object]:
    """Apply policy-only commands and retain chat presentation selectors."""

    commands = merge_commands(commands_from_selects(selects), updates)
    result = dict(selects)
    if commands:
        result[_OVERRIDES_KEY] = commands
    else:
        result.pop(_OVERRIDES_KEY, None)
    result.pop("model", None)
    result.pop("agic", None)
    result.pop("flow", None)
    result.pop("runnable", None)
    for command in commands:
        if command.group != "default" or not isinstance(command.value, str):
            continue
        if command.field == "model":
            result["model"] = command.value
            continue
        if command.field != "runnable":
            continue
        name, kind = parse_runnable_ref(command.value)
        result[kind or "runnable"] = name
    return result


def _text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    return value.strip() or None
