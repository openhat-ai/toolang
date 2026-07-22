"""Synchronous thread lifecycle management."""

from __future__ import annotations

from dataclasses import dataclass
import logging
import time

from toolang.base.types.message import Message
from toolang.common.files import file_write_lock
from toolang.common.ids import allocate_thread_id
from toolang.common.time import utc_now

from .events import (
    ThreadCreated,
    ThreadEvent,
    ThreadForked,
    ThreadListener,
    ThreadRewound,
)
from .executor import RunExecutor
from .records import (
    RunRecord,
    ThreadControlRecord,
    ThreadControlRef,
    ThreadPeer,
    ThreadRecord,
)

_LOGGER = logging.getLogger("toolang.thread")


@dataclass(frozen=True, slots=True)
class ThreadMutationResult:
    """One successful durable thread mutation."""

    thread: ThreadRecord
    control: ThreadControlRecord


class ThreadManager:
    """Create, fork, and rewind threads through durable controls."""

    def __init__(
        self,
        executor: RunExecutor,
        *,
        listener: ThreadListener | None = None,
        control_timeout: float = 30.0,
        control_poll_interval: float = 0.05,
    ) -> None:
        self.executor = executor
        self.store = executor.store
        self.listener = listener
        self.control_timeout = control_timeout
        self.control_poll_interval = control_poll_interval

    def create(
        self,
        *,
        kind: str = "chat",
        peer: ThreadPeer | None = None,
        request_id: str | None = None,
    ) -> ThreadMutationResult:
        """Create and persist one empty thread."""

        thread_id = allocate_thread_id(self.executor.id_state_path, kind)
        created_at = utc_now()
        thread, control, created = self.store.create_thread(
            thread_id=thread_id,
            origin="chat",
            peer=peer,
            request_id=request_id,
            context={"source": kind},
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
        return ThreadMutationResult(thread=thread, control=control)

    def fork(
        self,
        *,
        run_id: str,
        message: Message | None = None,
        request_id: str | None = None,
    ) -> ThreadMutationResult:
        """Fork a thread at one run without copying execution records."""

        anchor = self._branchable_run(run_id)
        source = self.store.get_thread(thread_id=anchor.thread)
        if source is None:
            raise RuntimeError(f"thread not found: {anchor.thread}")
        prefix = source.thread_id.split("_", 1)[0].strip() or "thread"
        thread_id = allocate_thread_id(self.executor.id_state_path, prefix)
        result_run = self.executor.allocate_run_id() if message is not None else None
        created_at = utc_now()
        thread, control, created = self.store.fork_thread(
            thread_id=thread_id,
            source_thread=source.thread_id,
            anchor_run=anchor.id,
            origin=source.origin,
            peer=source.peer,
            result_run=result_run,
            message=message,
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
        return ThreadMutationResult(thread=thread, control=control)

    def rewind(
        self,
        *,
        run_id: str,
        message: Message | None = None,
        request_id: str | None = None,
        expected_head: ThreadControlRef | None = None,
    ) -> ThreadMutationResult:
        """Synchronously stop affected runs and rewind their thread."""

        lock_path = self.store.db_path.with_name(
            f"{self.store.db_path.name}.threads.lock"
        )
        with file_write_lock(lock_path):
            return self._rewind_locked(
                run_id=run_id,
                message=message,
                request_id=request_id,
                expected_head=expected_head,
            )

    def _rewind_locked(
        self,
        *,
        run_id: str,
        message: Message | None,
        request_id: str | None,
        expected_head: ThreadControlRef | None,
    ) -> ThreadMutationResult:
        """Perform one rewind while holding the inter-process thread lock."""

        anchor = self._branchable_run(run_id)
        thread = self.store.get_thread(thread_id=anchor.thread)
        if thread is None:
            raise RuntimeError(f"thread not found: {anchor.thread}")
        if request_id is not None:
            replay = self.store.get_thread_control_by_request_id(request_id=request_id)
            if replay is not None:
                if (
                    replay.kind != "rewind"
                    or replay.thread != thread.thread_id
                    or replay.anchor_run != anchor.id
                    or replay.message != message
                    or replay.context
                ):
                    raise ValueError(
                        f"conflicting thread control request: {request_id}"
                    )
                return ThreadMutationResult(thread=thread, control=replay)
        expected = expected_head or thread.head
        if thread.head != expected:
            raise ValueError(f"thread head changed: {thread.thread_id}")
        result_run = self.executor.allocate_run_id() if message is not None else None
        self._stop_affected_runs(anchor)
        created_at = utc_now()
        updated, control, superseded, created = self.store.rewind_thread(
            thread_id=thread.thread_id,
            anchor_run=anchor.id,
            result_run=result_run,
            message=message,
            request_id=request_id,
            expected_head=expected,
            context={},
            created_at=created_at,
        )
        if created:
            self._notify(
                ThreadRewound(
                    thread=updated.thread_id,
                    control=ThreadControlRef(updated.thread_id, control.index),
                    anchor_run=anchor.id,
                    result_run=control.result_run,
                    superseded_runs=superseded,
                    created_at=created_at,
                )
            )
        return ThreadMutationResult(thread=updated, control=control)

    def _branchable_run(self, run_id: str) -> RunRecord:
        run = self.store.get_run(run_id=run_id)
        if run is None:
            raise FileNotFoundError(f"run not found: {run_id}")
        thread = self.store.get_thread(thread_id=run.thread)
        origin = thread.origin if thread is not None else run.origin
        if run.thread.startswith(("task_", "chore_")) or origin != "chat":
            raise ValueError(f"thread cannot be branched: {run.thread}")
        return run

    def _stop_affected_runs(self, anchor: RunRecord) -> None:
        active = tuple(
            run
            for run in self.store.list_runs_from_anchor(run_id=anchor.id)
            if run.status in {"pending", "running"}
        )
        for run in active:
            try:
                self.executor.stop(
                    run_id=run.id,
                    timing="immediate",
                    request_id=f"rewind:{anchor.id}:{run.id}",
                    reason="Run was rewound.",
                )
            except ValueError:
                current = self.store.get_run(run_id=run.id)
                if current is None or current.status in {"pending", "running"}:
                    raise
        deadline = time.monotonic() + self.control_timeout
        pending = {run.id for run in active}
        while pending:
            pending = {
                run_id
                for run_id in pending
                if (run := self.store.get_run(run_id=run_id)) is not None
                and run.status in {"pending", "running"}
            }
            if not pending:
                return
            if time.monotonic() >= deadline:
                names = ", ".join(sorted(pending))
                raise TimeoutError(f"runs did not stop before rewind: {names}")
            time.sleep(self.control_poll_interval)

    def _notify(self, event: ThreadEvent) -> None:
        if self.listener is None:
            return
        try:
            self.listener.on_event(event)
        except Exception:
            _LOGGER.exception("thread listener event handling failed")
