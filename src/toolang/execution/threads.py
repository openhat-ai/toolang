"""Synchronous thread lifecycle management."""

from __future__ import annotations

import logging
from pathlib import Path

from toolang.common.files import file_write_lock
from toolang.common.ids import IdIssuer
from toolang.common.time import utc_now

from .events import (
    ThreadCreated,
    ThreadEvent,
    ThreadForked,
    ThreadListener,
    ThreadRewound,
)
from .records import (
    ForkControlPayload,
    RewindControlPayload,
    ThreadPeer,
    ThreadRecord,
)
from .store import RunStore
from .types import ControlRef, ThreadPrefix

_LOGGER = logging.getLogger(__name__)


class ThreadManager:
    """Create, fork, and rewind threads through durable controls."""

    def __init__(
        self,
        store: RunStore,
        ids: IdIssuer,
        *,
        listener: ThreadListener | None = None,
    ) -> None:
        self.store = store
        self.ids = ids
        self.listener = listener

    def create(
        self,
        *,
        prefix: ThreadPrefix,
        request_id: str | None = None,
        peer: ThreadPeer | None = None,
    ) -> str:
        """Create an empty thread and return its id."""

        canonical_prefix = ThreadPrefix(prefix)
        thread_id = self.ids.issue_thread(canonical_prefix.value)
        created_at = utc_now()
        thread, control = self.store.create_thread(
            thread_id=thread_id,
            origin="script" if canonical_prefix is ThreadPrefix.SCRIPT else "chat",
            peer=peer,
            request_id=request_id,
            created_at=created_at,
        )
        self._notify(
            ThreadCreated(
                thread=thread.thread_id,
                control=ControlRef(thread.thread_id, control.index),
                origin=thread.origin,
                peer=thread.peer,
                created_at=created_at,
            )
        )
        return thread.thread_id

    def fork(
        self,
        *,
        thread_id: str,
        run_id: str | None = None,
        request_id: str | None = None,
    ) -> str:
        """Fork after one terminal visible run and return the new thread id."""

        with file_write_lock(self._lock_path):
            return self._fork_locked(
                thread_id=thread_id,
                run_id=run_id,
                request_id=request_id,
            )

    def rewind(
        self,
        *,
        thread_id: str,
        run_id: str | None = None,
        request_id: str | None = None,
    ) -> None:
        """Discard a terminal anchor and suffix from one idle thread."""

        with file_write_lock(self._lock_path):
            self._rewind_locked(
                thread_id=thread_id,
                run_id=run_id,
                request_id=request_id,
            )

    @property
    def _lock_path(self) -> Path:
        return self.store.thread_lock_path

    def _fork_locked(
        self,
        *,
        thread_id: str,
        run_id: str | None,
        request_id: str | None,
    ) -> str:
        source = self._branchable_thread(thread_id)
        try:
            prefix = ThreadPrefix(source.thread_id.split("_", 1)[0])
        except ValueError as exc:
            raise ValueError(
                f"thread has no issuable prefix: {source.thread_id}"
            ) from exc
        result_thread_id = self.ids.issue_thread(prefix.value)
        created_at = utc_now()
        thread, control = self.store.fork_thread(
            thread_id=result_thread_id,
            source=source.thread_id,
            anchor=run_id,
            request_id=request_id,
            created_at=created_at,
        )
        if not isinstance(control.payload, ForkControlPayload):
            raise RuntimeError(f"thread fork has no anchor: {thread.thread_id}")
        self._notify(
            ThreadForked(
                thread=thread.thread_id,
                control=ControlRef(thread.thread_id, control.index),
                source_thread=source.thread_id,
                anchor_run=control.payload.fork_at,
                created_at=created_at,
            )
        )
        return thread.thread_id

    def _rewind_locked(
        self,
        *,
        thread_id: str,
        run_id: str | None,
        request_id: str | None,
    ) -> None:
        thread = self._branchable_thread(thread_id)
        created_at = utc_now()
        updated, control, ejected = self.store.rewind_thread(
            thread_id=thread.thread_id,
            anchor=run_id,
            request_id=request_id,
            expected_head=thread.head,
            created_at=created_at,
        )
        if not isinstance(control.payload, RewindControlPayload):
            raise RuntimeError(f"thread rewind has no anchor: {updated.thread_id}")
        self._notify(
            ThreadRewound(
                thread=updated.thread_id,
                control=ControlRef(updated.thread_id, control.index),
                anchor_run=control.payload.rewind_from,
                ejected_runs=ejected,
                created_at=created_at,
            )
        )

    def _branchable_thread(self, thread_id: str) -> ThreadRecord:
        thread = self.store.get_thread(thread_id=thread_id)
        if thread is None:
            raise FileNotFoundError(f"thread not found: {thread_id}")
        if thread.origin != "chat":
            raise ValueError(f"thread cannot be branched: {thread.thread_id}")
        return thread

    def _notify(self, event: ThreadEvent) -> None:
        if self.listener is None:
            return
        try:
            self.listener.on_event(event)
        except Exception:
            _LOGGER.exception("thread listener event handling failed")
