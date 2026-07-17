"""Uptime adapter for immutable agent state updates."""

from __future__ import annotations

import asyncio
from dataclasses import replace
import logging
from typing import TYPE_CHECKING, cast

from toolang.execution.records import UpdateKind
from toolang.plugin.models.resolution import select_model_selectors
from toolang.state.agent import AgentState
from toolang.state.prepared import PreparedLocks, PreparedVisibility
from toolang.plugin.tools.loading import load_runtime_tools

if TYPE_CHECKING:
    from .context import ComponentState

logger = logging.getLogger("toolang.watch")
state_logger = logging.getLogger("toolang.state")


def spawn(
    context: ComponentState,
    *,
    stop_signal: asyncio.Event,
) -> asyncio.Task[None]:
    """Spawn the state watcher in one background task."""

    return asyncio.create_task(run(context, stop_signal=stop_signal))


async def run(context: ComponentState, *, stop_signal: asyncio.Event) -> None:
    """Apply new agent state versions to one running uptime."""

    interval_ms = _config_number(context, "components.trigger.watch.interval_ms")
    debounce_ms = _config_number(context, "components.trigger.watch.debounce_ms")
    watcher = context.state_watcher
    async for state in watcher.updates(
        stop_signal=stop_signal,
        interval_ms=interval_ms,
        debounce_ms=debounce_ms,
    ):
        _append_entry_change_updates(context, watcher.previous_locks, watcher.locks)
        _apply_state(context, state)


def _apply_state(context: ComponentState, state: AgentState) -> None:
    tools = load_runtime_tools(
        root=context.root,
        name=context.name,
        state=state,
        environ=context.executor.model_environ,
        selectors=_tool_allowed_selectors(context),
    )
    logger.debug(
        "watch.applied agent=%s state=%s->%s",
        context.name,
        _short_fingerprint(context.get_agent_state().fingerprint),
        _short_fingerprint(state.fingerprint),
    )
    context.executor.setup = replace(context.executor.setup, tools=tools)
    state_logger.info(
        "Agent reloaded state=%s models=%s tools=%s psyches=%s skills=%s services=%s",
        _short_fingerprint(state.fingerprint),
        _model_count(context),
        len(tools),
        _cap_count(state, "psyche"),
        _cap_count(state, "skill"),
        _cap_count(state, "service"),
    )


def _append_entry_change_updates(
    context: ComponentState,
    before: PreparedLocks | None,
    after: PreparedLocks | None,
) -> None:
    before_entries = _entry_change_snapshot(before)
    after_entries = _entry_change_snapshot(after)
    for visibility, kind, name in sorted(
        key
        for key in before_entries.keys() | after_entries.keys()
        if before_entries.get(key) != after_entries.get(key)
    ):
        context.store.append_update(
            kind=cast(UpdateKind, f"{kind}_changed"),
            payload={"name": name, "visibility": visibility},
        )


def _entry_change_snapshot(
    locks: PreparedLocks | None,
) -> dict[tuple[PreparedVisibility, str, str], tuple[str, str, str]]:
    if locks is None:
        return {}
    snapshot: dict[tuple[PreparedVisibility, str, str], tuple[str, str, str]] = {}
    for visibility, lock in (
        ("shared", locks.shared_lock),
        ("private", locks.private_lock),
    ):
        for entry in lock.entries:
            snapshot[(visibility, entry.kind, entry.name)] = (
                entry.ref,
                entry.source.fingerprint,
                entry.path,
            )
    return snapshot


def _config_number(context: ComponentState, key: str) -> float:
    value = context.config.require(key)
    if not isinstance(value, int | float):
        raise TypeError(f"invalid config: {key}")
    return float(value)


def _model_count(context: ComponentState) -> int:
    try:
        selectors = _model_allowed_selectors(context)
        if selectors:
            return len(
                select_model_selectors(
                    context.executor, activation_selectors=selectors
                )
            )
        return len(select_model_selectors(context.executor))
    except Exception:
        return len(_model_allowed_selectors(context))


def _model_allowed_selectors(context: ComponentState) -> tuple[str, ...]:
    return _config_strings(context, "models.allowed_selectors")


def _tool_allowed_selectors(context: ComponentState) -> tuple[str, ...] | None:
    value = context.config.get("tools.allowed_selectors")
    if value is None:
        return None
    return _config_strings(context, "tools.allowed_selectors")


def _config_strings(context: ComponentState, key: str) -> tuple[str, ...]:
    value = context.config.get(key)
    if not isinstance(value, tuple | list):
        return ()
    return tuple(item for item in value if isinstance(item, str) and item.strip())


def _cap_count(state: AgentState, kind: str) -> int:
    return sum(1 for entry in state.caps if entry.kind == kind)


def _short_fingerprint(value: str) -> str:
    return value[:12]
