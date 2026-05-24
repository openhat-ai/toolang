"""Watch feature that prepares and loads live state after durable changes."""

from __future__ import annotations

import asyncio
from dataclasses import replace
import hashlib
import logging
from pathlib import Path
import shutil
from typing import TYPE_CHECKING, cast

from watchfiles import Change, awatch

from ..caps import (
    build_visibility_lock,
    visibility_input_fingerprint,
    visibility_lock_content_fingerprint,
)
from ..progress import ProgressSink, emit_progress
from ..execution.records import UpdateKind
from ..state.durable import DurableState, is_durable_path, scan_durable_state
from ..state.live import load_live_state
from ..state.program import build_prepared_program
from ..state.prepared import (
    PreparedLock,
    PreparedState,
    PreparedVisibility,
    load_private_lock,
    load_shared_lock,
    load_prepared_state,
    write_prepared_lock,
)

if TYPE_CHECKING:
    from ..up import UptimeContext

DEFAULT_INTERVAL_MS = 1_000.0
DEFAULT_DEBOUNCE_MS = 500.0
logger = logging.getLogger("toolang.feature.watch")
_EMPTY_INPUT_FINGERPRINT = hashlib.sha256().hexdigest()


def spawn(
    context: UptimeContext,
    *,
    stop_signal: asyncio.Event,
) -> asyncio.Task[None]:
    """Spawn the watch feature in one background task."""

    return asyncio.create_task(run(context, stop_signal=stop_signal))


async def run(
    context: UptimeContext,
    *,
    stop_signal: asyncio.Event,
) -> None:
    """Watch durable inputs and keep the live state in sync."""

    reload_signal = asyncio.Event()
    prepare_task = asyncio.create_task(
        _run_prepare_watch(context, stop_signal=stop_signal, reload_signal=reload_signal)
    )
    load_task = asyncio.create_task(
        _run_load_live(context, stop_signal=stop_signal, reload_signal=reload_signal)
    )
    done, pending = await asyncio.wait(
        {prepare_task, load_task},
        return_when=asyncio.FIRST_EXCEPTION,
    )
    for task in pending:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    for task in done:
        task.result()


async def _run_prepare_watch(
    context: UptimeContext,
    *,
    stop_signal: asyncio.Event,
    reload_signal: asyncio.Event,
) -> None:
    """Watch durable inputs and produce new prepared locks."""
    interval_value = context.config.require("features.watch.interval_ms")
    if not isinstance(interval_value, int | float):
        raise TypeError("invalid config: features.watch.interval_ms")
    interval_ms = float(interval_value)
    context.root.mkdir(parents=True, exist_ok=True)
    logger.debug(
        "watch prepare started root=%s agent=%s interval_ms=%s",
        context.root,
        context.name,
        int(interval_ms),
    )
    async for changes in awatch(
        context.root,
        debounce=max(int(interval_ms), 50),
        step=50,
        stop_event=stop_signal,
    ):
        if not changes:
            continue
        changed_paths = {Path(path) for kind, path in changes if kind in _RELEVANT_CHANGES}
        apply_changes(context, changed_paths, reload_signal=reload_signal)


async def _run_load_live(
    context: UptimeContext,
    *,
    stop_signal: asyncio.Event,
    reload_signal: asyncio.Event,
) -> None:
    """Apply the latest prepared snapshot to live state after debounce."""

    debounce_value = context.config.require("features.watch.debounce_ms")
    if not isinstance(debounce_value, int | float):
        raise TypeError("invalid config: features.watch.debounce_ms")
    debounce_timeout = float(debounce_value) / 1000
    logger.debug(
        "watch load started root=%s agent=%s debounce_ms=%s live=%s",
        context.root,
        context.name,
        int(float(debounce_value)),
        _short_fingerprint(context.live.fingerprint),
    )
    while True:
        if not await _wait_for_reload_or_stop(reload_signal, stop_signal):
            return
        logger.info("watch reload requested agent=%s", context.name)
        if await _debounce_reload(reload_signal, stop_signal, debounce_timeout):
            return
        try:
            prepared = load_prepared_state(context.root, context.name)
        except FileNotFoundError:
            logger.debug("watch reload skipped missing prepared state agent=%s", context.name)
            continue
        if context.live.fingerprint == prepared.fingerprint:
            logger.debug(
                "watch reload skipped unchanged fingerprint=%s agent=%s",
                _short_fingerprint(prepared.fingerprint),
                context.name,
            )
            continue
        enabled_features = context.config.require("features.enabled")
        if not isinstance(enabled_features, tuple):
            raise TypeError("invalid config: features.enabled")
        live = load_live_state(prepared, enabled_features=cast(tuple[str, ...], enabled_features))
        from ..up import load_runtime_tool_plugins

        tools = load_runtime_tool_plugins(
            toolang_root=context.root,
            agent_name=context.name,
            live=live,
            environ=context.model_environ,
        )
        logger.info(
            "watch reload applied agent=%s live=%s->%s",
            context.name,
            _short_fingerprint(context.live.fingerprint),
            _short_fingerprint(live.fingerprint),
        )
        context.live = live
        context.tools = tools


def build_prepared_state(durable: DurableState, *, progress: ProgressSink | None = None) -> PreparedState:
    """Build and persist prepared locks for the current durable state."""

    current = _load_prepared_optional(durable.toolang_root, durable.agent_name)
    emit_progress(
        progress,
        id="prepare.state",
        phase="prepare.state",
        label="Prepare agent state",
        status="running",
        detail=durable.agent_name,
    )
    logger.debug(
        "prepare build started root=%s agent=%s durable_fingerprint=%s",
        durable.toolang_root,
        durable.agent_name,
        _short_fingerprint(durable.fingerprint),
    )
    _build_or_reuse_visibility_lock(
        durable,
        visibility="shared",
        current=current.shared_lock if current is not None else None,
        progress=progress,
    )
    _build_or_reuse_visibility_lock(
        durable,
        visibility="private",
        current=current.private_lock if current is not None else None,
        progress=progress,
    )
    prepared = load_prepared_state(durable.toolang_root, durable.agent_name)
    logger.debug(
        "prepare build completed root=%s agent=%s prepared_fingerprint=%s",
        durable.toolang_root,
        durable.agent_name,
        _short_fingerprint(prepared.fingerprint),
    )
    emit_progress(
        progress,
        id="prepare.state",
        phase="prepare.state",
        label="Prepare agent state",
        status="ok",
        detail=_short_fingerprint(prepared.fingerprint),
    )
    return prepared


def apply_changes(
    context: UptimeContext,
    changed_paths: set[Path],
    *,
    reload_signal: asyncio.Event,
) -> None:
    """Prepare durable changes and request one live reload when needed."""

    durable_relative_paths = _durable_relative_paths(context, changed_paths)
    durable_paths = [str(path) for path in durable_relative_paths]
    if not durable_paths:
        return
    logger.info(
        "prepare changed agent=%s paths=%s",
        context.name,
        ", ".join(durable_paths),
    )
    before = _load_prepared_optional(context.root, context.name)
    durable = scan_durable_state(context.root, context.name)
    prepared = build_prepared_state(durable)
    _append_entry_change_updates(context, before, prepared)
    should_reload = prepared.fingerprint != context.live.fingerprint
    logger.info(
        "prepare applied agent=%s shared=%s private=%s live=%s reload=%s",
        context.name,
        _lock_change(before, prepared, visibility="shared"),
        _lock_change(before, prepared, visibility="private"),
        _fingerprint_change(context.live.fingerprint, prepared.fingerprint),
        "yes" if should_reload else "no",
    )
    if should_reload:
        reload_signal.set()


_RELEVANT_CHANGES = {Change.added, Change.modified, Change.deleted}


def _write_visibility_if_changed(
    toolang_root: Path,
    lock: PreparedLock,
    files: dict[str, bytes],
    *,
    force: bool = False,
) -> PreparedLock:
    if _is_empty_shared_lock(lock, files):
        _remove_shared_prepared_dir(lock)
        return lock
    if not force:
        current = _load_lock_optional(lock)
        if (
            current is not None
            and current.fingerprint == lock.fingerprint
            and current.input_fingerprint == lock.input_fingerprint
        ):
            return current
    return write_prepared_lock(toolang_root, lock, files=files)


def _is_empty_shared_lock(lock: PreparedLock, files: dict[str, bytes]) -> bool:
    return (
        lock.visibility == "shared"
        and not lock.entries
        and not files
        and lock.input_fingerprint == _EMPTY_INPUT_FINGERPRINT
    )


def _remove_shared_prepared_dir(lock: PreparedLock) -> None:
    if lock.prepared_dir.name == ".caps" and lock.prepared_dir.exists():
        shutil.rmtree(lock.prepared_dir)


def _load_lock_optional(lock: PreparedLock) -> PreparedLock | None:
    try:
        if lock.visibility == "shared":
            return load_shared_lock(lock.lock_path.parent.parent)
        return load_private_lock(lock.lock_path.parents[3], lock.lock_path.parents[1].name)
    except (FileNotFoundError, KeyError, TypeError, ValueError):
        return None


def _load_prepared_optional(toolang_root: Path, agent_name: str) -> PreparedState | None:
    try:
        return load_prepared_state(toolang_root, agent_name)
    except (FileNotFoundError, KeyError, TypeError, ValueError):
        return None


def _build_or_reuse_visibility_lock(
    durable: DurableState,
    *,
    visibility: PreparedVisibility,
    current: PreparedLock | None,
    progress: ProgressSink | None,
) -> PreparedLock:
    if current is not None and _visibility_lock_matches(durable, visibility=visibility, lock=current):
        emit_progress(
            progress,
            id=f"prepare.visibility:{visibility}",
            phase="prepare.visibility",
            label=f"Prepare {visibility} caps",
            status="ok",
            detail="cached",
        )
        return current
    lock, files = build_visibility_lock(durable, visibility=visibility, progress=progress)
    if visibility == "private":
        program = build_prepared_program(durable)
        lock = replace(
            lock,
            fingerprint=_combined_private_fingerprint(lock.fingerprint, program.fingerprint()),
            program=program,
        )
    return _write_visibility_if_changed(durable.toolang_root, lock, files, force=True)


def _visibility_lock_matches(
    durable: DurableState,
    *,
    visibility: PreparedVisibility,
    lock: PreparedLock,
) -> bool:
    if lock.input_fingerprint != visibility_input_fingerprint(durable, visibility=visibility):
        return False
    if visibility == "private" and lock.program is None:
        return False
    try:
        fingerprint = visibility_lock_content_fingerprint(durable.toolang_root, lock)
    except (FileNotFoundError, KeyError):
        return False
    if visibility == "private":
        if lock.program is None:
            return False
        fingerprint = _combined_private_fingerprint(fingerprint, lock.program.fingerprint())
    return fingerprint == lock.fingerprint


def _append_entry_change_updates(
    context: UptimeContext,
    before: PreparedState | None,
    after: PreparedState,
) -> None:
    before_entries = _entry_change_snapshot(before)
    after_entries = _entry_change_snapshot(after)
    changed_keys = sorted(
        key
        for key in before_entries.keys() | after_entries.keys()
        if before_entries.get(key) != after_entries.get(key)
    )
    for visibility, kind, name in changed_keys:
        context.store.append_update(
            kind=cast(UpdateKind, f"{kind}_changed"),
            payload={
                "name": name,
                "visibility": visibility,
            },
        )


def _entry_change_snapshot(
    prepared: PreparedState | None,
) -> dict[tuple[PreparedVisibility, str, str], tuple[str, str, str]]:
    if prepared is None:
        return {}
    snapshot: dict[tuple[PreparedVisibility, str, str], tuple[str, str, str]] = {}
    for visibility, lock in (("shared", prepared.shared_lock), ("private", prepared.private_lock)):
        for entry in lock.entries:
            snapshot[(visibility, entry.kind, entry.name)] = (
                entry.ref,
                entry.source.fingerprint,
                entry.path,
            )
    return snapshot


def _durable_relative_paths(context: UptimeContext, changed_paths: set[Path]) -> list[Path]:
    relative_paths: list[Path] = []
    for path in changed_paths:
        if not is_durable_path(context.root, context.name, path):
            continue
        try:
            relative_paths.append(path.relative_to(context.root))
        except ValueError:
            relative_paths.append(path)
    compacted: list[Path] = []
    for path in sorted(relative_paths, key=lambda item: (len(item.parts), item.as_posix())):
        if any(_is_parent(parent, path) for parent in compacted):
            continue
        compacted.append(path)
    return compacted


def _is_parent(parent: Path, child: Path) -> bool:
    return parent == child or parent in child.parents


def _lock_change(
    before: PreparedState | None,
    after: PreparedState,
    *,
    visibility: PreparedVisibility,
) -> str:
    if visibility == "shared":
        before_lock = before.shared_lock if before is not None else None
        after_lock = after.shared_lock
    else:
        before_lock = before.private_lock if before is not None else None
        after_lock = after.private_lock
    before_fingerprint = before_lock.fingerprint if before_lock is not None else None
    return _fingerprint_change(before_fingerprint, after_lock.fingerprint)


def _fingerprint_change(before: str | None, after: str) -> str:
    after_short = _short_fingerprint(after)
    if before is None:
        return f"new->{after_short}"
    before_short = _short_fingerprint(before)
    if before == after:
        return before_short
    return f"{before_short}->{after_short}"


def _short_fingerprint(value: str) -> str:
    return value[:12]


def _combined_private_fingerprint(lock_fingerprint: str, program_fingerprint: str) -> str:
    import hashlib

    digest = hashlib.sha256()
    digest.update(lock_fingerprint.encode("utf-8"))
    digest.update(b"\0")
    digest.update(program_fingerprint.encode("utf-8"))
    return digest.hexdigest()


async def _wait_for_reload_or_stop(
    reload_signal: asyncio.Event,
    stop_signal: asyncio.Event,
) -> bool:
    reload_task = asyncio.create_task(reload_signal.wait())
    stop_task = asyncio.create_task(stop_signal.wait())
    done, pending = await asyncio.wait(
        {reload_task, stop_task},
        return_when=asyncio.FIRST_COMPLETED,
    )
    for task in pending:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    return reload_task in done


async def _debounce_reload(
    reload_signal: asyncio.Event,
    stop_signal: asyncio.Event,
    debounce_timeout: float,
) -> bool:
    while True:
        reload_signal.clear()
        try:
            await asyncio.wait_for(stop_signal.wait(), timeout=debounce_timeout)
            return True
        except TimeoutError:
            if reload_signal.is_set():
                logger.debug("watch reload coalesced additional signals")
                continue
            return False
