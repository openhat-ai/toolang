"""Shared terminal-chat policy state operations."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import cast

from toolang.base.types.model import (
    ModelParameters,
    ModelRequest,
    ReasoningEffort,
    ReasoningParameters,
)
from toolang.base.types.policy import RunBindings, RunLimits, RunPolicy
from toolang.execution.policy import materialize_policy, merge_commands
from toolang.execution.runnables import parse_runnable_ref
from toolang.execution.schemas import RunRequest, RunnableRequest
from toolang.execution.types import RunOverride
from toolang.lang.input import RunnableInputRaw

_OVERRIDES_KEY = "run_overrides"
_REASONING_EFFORT_KEY = "reasoning_effort"


@dataclass(frozen=True, slots=True)
class ChatRunDefaults:
    """Concrete server defaults adopted by one Chat session."""

    bindings: RunBindings
    limits: RunLimits


def build_run_request(
    *,
    thread_id: str,
    request_id: str,
    input: RunnableInputRaw,
    input_commands: Sequence[RunOverride],
    selects: Mapping[str, object],
    defaults: ChatRunDefaults,
    resolve_model_ref: Callable[[str], str],
    resolve_runnable_ref: Callable[[str], str],
) -> RunRequest:
    """Snapshot Chat state and input-local overrides into one run request."""

    ceilings, bindings, limits = materialize_policy(
        defaults.bindings,
        defaults.limits,
        session=commands_from_selects(selects),
        run=input_commands,
    )
    if bindings.runnable is None:
        raise ValueError("chat session has no runnable")
    runnable_ref = resolve_runnable_ref(bindings.runnable)
    selected_model = _text(selects.get("model"))
    effort = (
        reasoning_effort_from_selects(selects)
        if bindings.model == selected_model
        else None
    )
    model = (
        ModelRequest(
            resolve_model_ref(bindings.model),
            ModelParameters(
                reasoning=(
                    ReasoningParameters(effort=effort) if effort is not None else None
                )
            ),
        )
        if bindings.model is not None
        else None
    )
    return RunRequest(
        thread_id=thread_id,
        request_id=request_id,
        runnable=RunnableRequest(runnable_ref, input),
        model=model,
        policy=RunPolicy(allow=ceilings, limits=limits),
    )


def reasoning_effort_from_selects(
    selects: Mapping[str, object],
) -> ReasoningEffort | None:
    value = _text(selects.get(_REASONING_EFFORT_KEY))
    if value in {"none", "minimal", "low", "medium", "high", "xhigh", "max", "default"}:
        return cast(ReasoningEffort, value)
    return None


def materialize_runnable_list_ref(
    payload: Mapping[str, object],
    selector: str,
) -> str:
    """Resolve one runnable selector to a kind-qualified list ref."""

    requested_name, requested_kind = parse_runnable_ref(selector)
    raw_items = payload.get("items")
    items = raw_items if isinstance(raw_items, list | tuple) else ()
    matches: list[str] = []
    for raw in items:
        if not isinstance(raw, Mapping):
            continue
        item = cast(Mapping[str, object], raw)
        name = _text(item.get("name"))
        kind = _text(item.get("kind"))
        if name != requested_name or kind not in {"agic", "flow"}:
            continue
        if requested_kind is not None and requested_kind != kind:
            continue
        matches.append(f"{kind}:{name}")
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise ValueError(f"runnable ref did not match an available route: {selector}")
    joined = ", ".join(matches)
    raise ValueError(f"runnable ref is ambiguous: {selector} (matches {joined})")


def apply_model_selection(
    selects: Mapping[str, object],
    *,
    ref: str,
    effort: ReasoningEffort | None,
) -> dict[str, object]:
    """Atomically update the session model and reasoning effort."""

    result = apply_session_commands(
        selects,
        (RunOverride("default", "model", ref),),
    )
    if effort is None:
        result.pop(_REASONING_EFFORT_KEY, None)
    else:
        result[_REASONING_EFFORT_KEY] = effort
    return result


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

    previous_model = _text(selects.get("model"))
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
        try:
            name, kind = parse_runnable_ref(command.value)
        except ValueError:
            result["runnable"] = command.value
        else:
            result[kind or "runnable"] = name
    if _text(result.get("model")) != previous_model:
        result.pop(_REASONING_EFFORT_KEY, None)
    return result


def _text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    return value.strip() or None
