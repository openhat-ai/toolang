"""Uptime adapter for immutable agent state updates."""

from __future__ import annotations

import asyncio
from dataclasses import replace
import logging
from pathlib import Path
from typing import cast

from toolang.config.runtime import RuntimeConfig
from toolang.config.files import load_named_config
from toolang.execution.executor import Executor
from toolang.execution.records import UpdateKind
from toolang.execution.store import RunStore
from toolang.plugin.models.resolution import select_model_selectors
from toolang.state.agent import AgentState
from toolang.state.prepared import PreparedLocks, PreparedVisibility
from toolang.plugin.tools.loading import load_runtime_tools
from toolang.state.watcher import StateWatcher

logger = logging.getLogger("toolang.watch")
state_logger = logging.getLogger("toolang.state")


def spawn(
    *,
    root: Path,
    name: str,
    watcher: StateWatcher,
    executor: Executor,
    store: RunStore,
    config: RuntimeConfig,
    stop_signal: asyncio.Event,
) -> asyncio.Task[None]:
    """Spawn the state watcher in one background task."""

    return asyncio.create_task(
        run(
            root=root,
            name=name,
            watcher=watcher,
            executor=executor,
            store=store,
            config=config,
            stop_signal=stop_signal,
        )
    )


async def run(
    *,
    root: Path,
    name: str,
    watcher: StateWatcher,
    executor: Executor,
    store: RunStore,
    config: RuntimeConfig,
    stop_signal: asyncio.Event,
) -> None:
    """Apply new agent state versions to one running uptime."""

    interval_ms = _config_number(config, "components.trigger.watch.interval_ms")
    debounce_ms = _config_number(config, "components.trigger.watch.debounce_ms")
    previous = watcher.current()
    async for state in watcher.updates(
        stop_signal=stop_signal,
        interval_ms=interval_ms,
        debounce_ms=debounce_ms,
    ):
        _append_entry_change_updates(store, watcher.previous_locks, watcher.locks)
        _apply_state(
            root=root,
            name=name,
            previous=previous,
            state=state,
            executor=executor,
            config=config,
        )
        previous = state


def _apply_state(
    *,
    root: Path,
    name: str,
    previous: AgentState,
    state: AgentState,
    executor: Executor,
    config: RuntimeConfig,
) -> None:
    tools = load_runtime_tools(
        plugin_config=load_named_config(
            root,
            name,
            section="tools",
            environ=executor.model_environ,
        ),
        entries=state.caps,
        selectors=_tool_allowed_selectors(config),
    )
    logger.debug(
        "watch.applied agent=%s state=%s->%s",
        name,
        _short_fingerprint(previous.fingerprint),
        _short_fingerprint(state.fingerprint),
    )
    executor.setup = replace(executor.setup, tools=tools)
    state_logger.info(
        "Agent reloaded state=%s models=%s tools=%s psyches=%s skills=%s services=%s",
        _short_fingerprint(state.fingerprint),
        _model_count(executor, config),
        len(tools),
        _cap_count(state, "psyche"),
        _cap_count(state, "skill"),
        _cap_count(state, "service"),
    )


def _append_entry_change_updates(
    store: RunStore,
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
        store.append_update(
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


def _config_number(config: RuntimeConfig, key: str) -> float:
    value = config.require(key)
    if not isinstance(value, int | float):
        raise TypeError(f"invalid config: {key}")
    return float(value)


def _model_count(executor: Executor, config: RuntimeConfig) -> int:
    try:
        selectors = _model_allowed_selectors(config)
        if selectors:
            return len(
                select_model_selectors(
                    executor, activation_selectors=selectors
                )
            )
        return len(select_model_selectors(executor))
    except Exception:
        return len(_model_allowed_selectors(config))


def _model_allowed_selectors(config: RuntimeConfig) -> tuple[str, ...]:
    return _config_strings(config, "models.allowed_selectors")


def _tool_allowed_selectors(config: RuntimeConfig) -> tuple[str, ...] | None:
    value = config.get("tools.allowed_selectors")
    if value is None:
        return None
    return _config_strings(config, "tools.allowed_selectors")


def _config_strings(config: RuntimeConfig, key: str) -> tuple[str, ...]:
    value = config.get(key)
    if not isinstance(value, tuple | list):
        return ()
    return tuple(item for item in value if isinstance(item, str) and item.strip())


def _cap_count(state: AgentState, kind: str) -> int:
    return sum(1 for entry in state.caps if entry.kind == kind)


def _short_fingerprint(value: str) -> str:
    return value[:12]
