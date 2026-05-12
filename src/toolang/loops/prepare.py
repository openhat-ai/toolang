"""Prepare loop that watches durable changes and writes prepared locks."""

from __future__ import annotations

import asyncio
from dataclasses import replace
import logging
from pathlib import Path
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
logger = logging.getLogger("toolang.loop.prepare")


def spawn(
    context: UptimeContext,
    *,
    stop_signal: asyncio.Event,
    reload_signal: asyncio.Event,
) -> asyncio.Task[None]:
    """Spawn the prepare loop in one background task."""

    return asyncio.create_task(run(context, stop_signal=stop_signal, reload_signal=reload_signal))


async def run(
    context: UptimeContext,
    *,
    stop_signal: asyncio.Event,
    reload_signal: asyncio.Event,
) -> None:
    """Watch durable inputs and produce new prepared locks."""
    interval_value = context.config.require("loops.prepare.interval_ms")
    if not isinstance(interval_value, int | float):
        raise TypeError("invalid config: loops.prepare.interval_ms")
    interval_ms = float(interval_value)
    context.root.mkdir(parents=True, exist_ok=True)
    logger.debug(
        "prepare loop started root=%s agent=%s interval_ms=%s",
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
    current = _load_lock_optional(lock)
    if (
        not force
        and current is not None
        and current.fingerprint == lock.fingerprint
        and current.input_fingerprint == lock.input_fingerprint
    ):
        return current
    return write_prepared_lock(toolang_root, lock, files=files)


def _load_lock_optional(lock: PreparedLock) -> PreparedLock | None:
    try:
        if lock.visibility == "shared":
            return load_shared_lock(lock.lock_path.parent.parent)
        return load_private_lock(lock.lock_path.parents[3], lock.lock_path.parents[1].name)
    except FileNotFoundError:
        return None


def _load_prepared_optional(toolang_root: Path, agent_name: str) -> PreparedState | None:
    try:
        return load_prepared_state(toolang_root, agent_name)
    except FileNotFoundError:
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
