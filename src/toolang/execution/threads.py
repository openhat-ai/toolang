"""Synchronous thread lifecycle management."""

from __future__ import annotations

import logging
from pathlib import Path
import time

from toolang.base.types.message import Message
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
    RunRecord,
    ThreadControlRecord,
    ThreadControlRef,
    ThreadPeer,
    ThreadRecord,
)
from .store import RunStore
from .types import ThreadControlKind, ThreadPrefix

_LOGGER = logging.getLogger("toolang.thread")
_CONTROL_TIMEOUT = 30.0
_CONTROL_POLL_INTERVAL = 0.05


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
        """Create an empty chat thread and return its id."""

        canonical_prefix = ThreadPrefix(prefix)
        thread_id = self.ids.issue_thread(canonical_prefix.value)
        created_at = utc_now()
        thread, control, created = self.store.create_thread(
            thread_id=thread_id,
            origin="chat",
            peer=peer,
            request_id=request_id,
            context={"prefix": canonical_prefix.value},
            created_at=created_at,
        )
        if created:
            self._notify(
                ThreadCreated(
                    thread=thread.thread_id,
                    control=ThreadControlRef(thread.thread_id, control.index),
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
        """Fork after one visible run and return the new thread id."""

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
        """Synchronously discard one visible run and the suffix after it."""

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
        replay = self._replayed_control(
            kind="fork",
            thread_id=thread_id,
            run_id=run_id,
            request_id=request_id,
        )
        if replay is not None:
            return replay.thread
        source = self._branchable_thread(thread_id)
        anchor = self._visible_anchor(thread_id, run_id)
        try:
            prefix = ThreadPrefix(source.thread_id.split("_", 1)[0])
        except ValueError as exc:
            raise ValueError(
                f"thread has no issuable prefix: {source.thread_id}"
            ) from exc
        result_thread_id = self.ids.issue_thread(prefix.value)
        created_at = utc_now()
        thread, control, created = self.store.fork_thread(
            thread_id=result_thread_id,
            source_thread=source.thread_id,
            anchor_run=anchor.id,
            origin=source.origin,
            peer=source.peer,
            request_id=request_id,
            context={},
            created_at=created_at,
        )
        if created:
            self._notify(
                ThreadForked(
                    thread=thread.thread_id,
                    control=ThreadControlRef(thread.thread_id, control.index),
                    source_thread=source.thread_id,
                    anchor_run=anchor.id,
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
        replay = self._replayed_control(
            kind="rewind",
            thread_id=thread_id,
            run_id=run_id,
            request_id=request_id,
        )
        if replay is not None:
            return
        thread = self._branchable_thread(thread_id)
        anchor = self._visible_anchor(thread_id, run_id)
        self._stop_affected_runs(thread_id, anchor)
        created_at = utc_now()
        updated, control, superseded, created = self.store.rewind_thread(
            thread_id=thread.thread_id,
            anchor_run=anchor.id,
            request_id=request_id,
            expected_head=thread.head,
            context={},
            created_at=created_at,
        )
        if created:
            self._notify(
                ThreadRewound(
                    thread=updated.thread_id,
                    control=ThreadControlRef(updated.thread_id, control.index),
                    anchor_run=anchor.id,
                    superseded_runs=superseded,
                    created_at=created_at,
                )
            )

    def _replayed_control(
        self,
        *,
        kind: ThreadControlKind,
        thread_id: str,
        run_id: str | None,
        request_id: str | None,
    ) -> ThreadControlRecord | None:
        if request_id is None:
            return None
        control = self.store.get_thread_control_by_request_id(request_id=request_id)
        if control is None:
            return None
        source_thread = control.source_thread if kind == "fork" else control.thread
        if (
            control.kind != kind
            or source_thread != thread_id
            or (run_id is not None and control.anchor_run != run_id)
        ):
            raise ValueError(f"conflicting thread control request: {request_id}")
        return control

    def _branchable_thread(self, thread_id: str) -> ThreadRecord:
        thread = self.store.get_thread(thread_id=thread_id)
        if thread is None:
            raise FileNotFoundError(f"thread not found: {thread_id}")
        if thread.origin != "chat":
            raise ValueError(f"thread cannot be branched: {thread.thread_id}")
        return thread

    def _visible_anchor(self, thread_id: str, run_id: str | None) -> RunRecord:
        history = self._visible_runs(thread_id)
        if not history:
            raise ValueError(f"thread has no runs: {thread_id}")
        if run_id is None:
            return history[-1]
        anchor = next((run for run in history if run.id == run_id), None)
        if anchor is None:
            raise ValueError(f"run is not visible in thread {thread_id}: {run_id}")
        return anchor

    def _stop_affected_runs(self, thread_id: str, anchor: RunRecord) -> None:
        history = self._visible_runs(thread_id)
        anchor_index = next(
            (index for index, run in enumerate(history) if run.id == anchor.id),
            None,
        )
        if anchor_index is None:
            raise ValueError(f"run is not visible in thread {thread_id}: {anchor.id}")
        active = tuple(
            run
            for run in history[anchor_index:]
            if run.thread == thread_id and run.status in {"pending", "running"}
        )
        for run in active:
            try:
                self.store.accept_run_control(
                    run_id=run.id,
                    kind="stop",
                    timing="immediate",
                    input=Message.user("Run was rewound."),
                    context={},
                    request_id=f"rewind:{thread_id}:{anchor.id}:{run.id}",
                    created_at=utc_now(),
                )
            except ValueError:
                current = self.store.get_run(run_id=run.id)
                if current is None or current.status in {"pending", "running"}:
                    raise
        deadline = time.monotonic() + _CONTROL_TIMEOUT
        pending = {run.id for run in active}
        while pending:
            pending = {
                candidate
                for candidate in pending
                if (run := self.store.get_run(run_id=candidate)) is not None
                and run.status in {"pending", "running"}
            }
            if not pending:
                return
            if time.monotonic() >= deadline:
                names = ", ".join(sorted(pending))
                raise TimeoutError(f"runs did not stop before rewind: {names}")
            time.sleep(_CONTROL_POLL_INTERVAL)

    def _visible_runs(self, thread_id: str) -> tuple[RunRecord, ...]:
        return tuple(
            run
            for run in self.store.list_thread_history_chronological(
                thread_id=thread_id
            )
            if run.parent is None
        )

    def _notify(self, event: ThreadEvent) -> None:
        if self.listener is None:
            return
        try:
            self.listener.on_event(event)
        except Exception:
            _LOGGER.exception("thread listener event handling failed")
