"""Prepare and watch immutable agent source state."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import contextmanager
from dataclasses import replace
import fcntl
import hashlib
import logging
from pathlib import Path
import shutil

from watchfiles import Change, awatch

from toolang.catalog.cap import (
    build_visibility_lock,
    remote_entry_cache,
    visibility_input_fingerprint,
    visibility_lock_content_fingerprint,
)
from ..common.progress import ProgressSink, emit_progress
from .agent import AgentState, load_agent_state
from toolang.state.durable import DurableState, is_durable_path, scan_durable_state
from toolang.state.prepared import (
    PreparedLock,
    PreparedLocks,
    PreparedVisibility,
    load_private_lock,
    load_shared_lock,
    load_prepared_locks,
    write_prepared_lock,
)

DEFAULT_INTERVAL_MS = 1_000.0
DEFAULT_DEBOUNCE_MS = 500.0
logger = logging.getLogger("toolang.watch")
prepare_logger = logging.getLogger("toolang.prepare")
_EMPTY_INPUT_FINGERPRINT = hashlib.sha256().hexdigest()
_RELEVANT_CHANGES = {Change.added, Change.modified, Change.deleted}


class StateWatcher:
    """Publish new immutable agent state when authored files change."""

    def __init__(
        self,
        root: Path,
        name: str,
        state: AgentState,
        *,
        transform: Callable[[AgentState], AgentState] | None = None,
    ) -> None:
        self.root = root
        self.name = name
        self._state = state
        self._transform = transform or (lambda value: value)
        self.locks = _load_prepared_optional(root, name)
        self.previous_locks: PreparedLocks | None = None

    def current(self) -> AgentState:
        return self._state

    def refresh(self) -> AgentState:
        previous_locks = self.locks
        self.previous_locks = previous_locks
        locks = prepare_locks(scan_durable_state(self.root, self.name))
        self.locks = locks
        program = (
            self._state.program
            if previous_locks is not None
            and locks.program_source.fingerprint()
            == previous_locks.program_source.fingerprint()
            else None
        )
        self._state = self._transform(load_agent_state(locks, program=program))
        return self._state

    async def updates(
        self,
        *,
        stop_signal: asyncio.Event,
        interval_ms: float = DEFAULT_INTERVAL_MS,
        debounce_ms: float = DEFAULT_DEBOUNCE_MS,
    ) -> AsyncIterator[AgentState]:
        self.root.mkdir(parents=True, exist_ok=True)
        logger.debug(
            "watch.started root=%s agent=%s interval_ms=%s debounce_ms=%s",
            self.root,
            self.name,
            int(interval_ms),
            int(debounce_ms),
        )
        async for changes in awatch(
            self.root,
            debounce=max(int(debounce_ms), 50),
            step=max(int(interval_ms), 50),
            stop_event=stop_signal,
        ):
            paths = {
                Path(path)
                for kind, path in changes
                if kind in _RELEVANT_CHANGES
                and is_durable_path(self.root, self.name, Path(path))
            }
            if not paths:
                continue
            previous = self._state.fingerprint
            state = self.refresh()
            if state.fingerprint != previous:
                yield state


def prepare_locks(
    durable: DurableState, *, progress: ProgressSink | None = None
) -> PreparedLocks:
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
    prepare_logger.debug(
        "prepare.started root=%s agent=%s durable_fingerprint=%s",
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
    prepared = load_prepared_locks(durable.toolang_root, durable.agent_name)
    prepare_logger.debug(
        "prepare.result root=%s agent=%s prepared_fingerprint=%s",
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
        return load_private_lock(
            lock.lock_path.parents[3], lock.lock_path.parents[1].name
        )
    except (FileNotFoundError, KeyError, TypeError, ValueError):
        return None


def _load_prepared_optional(
    toolang_root: Path, agent_name: str
) -> PreparedLocks | None:
    try:
        return load_prepared_locks(toolang_root, agent_name)
    except (FileNotFoundError, KeyError, TypeError, ValueError):
        return None


def _build_or_reuse_visibility_lock(
    durable: DurableState,
    *,
    visibility: PreparedVisibility,
    current: PreparedLock | None,
    progress: ProgressSink | None,
) -> PreparedLock:
    if current is not None and _visibility_lock_matches(
        durable, visibility=visibility, lock=current
    ):
        emit_progress(
            progress,
            id=f"prepare.visibility:{visibility}",
            phase="prepare.visibility",
            label=f"Prepare {visibility} caps",
            status="ok",
            detail="cached",
        )
        return current
    if visibility == "shared":
        with _shared_prepare_lock(durable.toolang_root):
            latest = _latest_visibility_lock(durable, visibility=visibility)
            if latest is not None and _visibility_lock_matches(
                durable, visibility=visibility, lock=latest
            ):
                emit_progress(
                    progress,
                    id=f"prepare.visibility:{visibility}",
                    phase="prepare.visibility",
                    label=f"Prepare {visibility} caps",
                    status="ok",
                    detail="cached",
                )
                return latest
            return _build_visibility_lock(
                durable,
                visibility=visibility,
                current=latest or current,
                progress=progress,
            )
    return _build_visibility_lock(
        durable, visibility=visibility, current=current, progress=progress
    )


def _build_visibility_lock(
    durable: DurableState,
    *,
    visibility: PreparedVisibility,
    current: PreparedLock | None,
    progress: ProgressSink | None,
) -> PreparedLock:
    cache = (
        remote_entry_cache(durable.toolang_root, current)
        if current is not None
        else None
    )
    lock, files = build_visibility_lock(
        durable, visibility=visibility, remote_cache=cache, progress=progress
    )
    if visibility == "private":
        program_source = durable.load_program()
        lock = replace(
            lock,
            fingerprint=_combined_private_fingerprint(
                lock.fingerprint, program_source.fingerprint()
            ),
            program_source=program_source,
        )
    return _write_visibility_if_changed(durable.toolang_root, lock, files, force=True)


@contextmanager
def _shared_prepare_lock(toolang_root: Path) -> Iterator[None]:
    toolang_root.mkdir(parents=True, exist_ok=True)
    lock_path = toolang_root / ".prepare-shared.lock"
    with lock_path.open("a+b") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _latest_visibility_lock(
    durable: DurableState,
    *,
    visibility: PreparedVisibility,
) -> PreparedLock | None:
    try:
        if visibility == "shared":
            return load_shared_lock(durable.toolang_root)
        return load_private_lock(durable.toolang_root, durable.agent_name)
    except (FileNotFoundError, KeyError, TypeError, ValueError):
        return None


def _visibility_lock_matches(
    durable: DurableState,
    *,
    visibility: PreparedVisibility,
    lock: PreparedLock,
) -> bool:
    if lock.input_fingerprint != visibility_input_fingerprint(
        durable, visibility=visibility
    ):
        return False
    return _visibility_lock_outputs_match(durable.toolang_root, lock)


def _visibility_lock_outputs_match(toolang_root: Path, lock: PreparedLock) -> bool:
    if lock.visibility == "private" and lock.program_source is None:
        return False
    try:
        fingerprint = visibility_lock_content_fingerprint(toolang_root, lock)
    except (FileNotFoundError, KeyError):
        return False
    if lock.visibility == "private":
        if lock.program_source is None:
            return False
        fingerprint = _combined_private_fingerprint(
            fingerprint, lock.program_source.fingerprint()
        )
    return fingerprint == lock.fingerprint


def _short_fingerprint(value: str) -> str:
    return value[:12]


def _combined_private_fingerprint(
    lock_fingerprint: str, program_fingerprint: str
) -> str:
    import hashlib

    digest = hashlib.sha256()
    digest.update(lock_fingerprint.encode("utf-8"))
    digest.update(b"\0")
    digest.update(program_fingerprint.encode("utf-8"))
    return digest.hexdigest()
