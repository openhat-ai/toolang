"""Synchronous thread lifecycle management."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import TypeVar

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
from .stream import CanonicalStream
from .types import ThreadPrefix
from .types import ConversationScope, ControlRef

_LOGGER = logging.getLogger(__name__)
_Result = TypeVar("_Result")


class ThreadManager:
    """Create, fork, and rewind threads through durable controls."""

    def __init__(
        self,
        store: RunStore,
        ids: IdIssuer,
        *,
        listener: ThreadListener | None = None,
        stream: CanonicalStream | None = None,
    ) -> None:
        self.store = store
        self.ids = ids
        self.listener = listener
        self.stream = stream
        self._mutation_lock = threading.RLock()

    def _apply(
        self, operation: Callable[[], tuple[_Result, ThreadEvent | None]]
    ) -> _Result:
        if self.stream is not None:
            with self.stream.publication() as publication:
                with self.store.write_transaction():
                    result, event = operation()
                    if event is not None:
                        self.store.record_event_cursor(
                            event.control, publication.cursor
                        )
                        publication.append(event)
            return result
        # Commit before callbacks, keeping one manager's listener delivery ordered.
        with self._mutation_lock:
            with self.store.write_transaction():
                result, event = operation()
            if event is not None:
                self._notify(event)
        return result

    def create(
        self,
        *,
        prefix: ThreadPrefix,
        request_id: str | None = None,
        client_id: str | None = None,
        peer: ThreadPeer | None = None,
    ) -> str:
        """Create an empty thread and return its id."""

        canonical_prefix = ThreadPrefix(prefix)
        thread_id = self.ids.issue_thread(canonical_prefix.value)
        created_at = utc_now()

        def operation() -> tuple[str, ThreadEvent]:
            thread, control = self.store.create_thread(
                thread_id=thread_id,
                origin=canonical_prefix.value,
                peer=peer,
                request_id=request_id,
                client_id=client_id,
                created_at=created_at,
            )
            event = ThreadCreated(
                thread=thread.id,
                control=control.ref,
                origin=thread.origin,
                peer=thread.peer,
                created_at=created_at,
            )
            return thread.id, event

        return self._apply(operation)

    def ensure_talk(self, scope: ConversationScope, *, thread_id: str) -> str:
        """Ensure one explicit conversation binding without adopting old threads."""

        def operation() -> tuple[str, ThreadEvent | None]:
            if not self.store.talk.bind(scope, thread_id):
                return thread_id, None
            thread = self.store.get_thread(thread_id=thread_id)
            assert thread is not None
            return thread_id, ThreadCreated(
                thread=thread_id,
                control=ControlRef.for_thread(thread_id, 0),
                origin=thread.origin,
                peer=thread.peer,
                created_at=thread.created_at,
            )

        return self._apply(operation)

    def fork(
        self,
        *,
        thread_id: str,
        run_id: str | None = None,
        request_id: str | None = None,
        client_id: str | None = None,
    ) -> str:
        """Fork after one terminal visible run and return the new thread id."""

        source = self._branchable_thread(thread_id)
        try:
            prefix = ThreadPrefix(source.id.split("_", 1)[0])
        except ValueError as exc:
            raise ValueError(f"thread has no issuable prefix: {source.id}") from exc
        result_thread_id = self.ids.issue_thread(prefix.value)
        created_at = utc_now()

        def operation() -> tuple[str, ThreadEvent]:
            source = self._branchable_thread(thread_id)
            thread, control = self.store.fork_thread(
                thread_id=result_thread_id,
                source=source.id,
                anchor=run_id,
                request_id=request_id,
                client_id=client_id,
                created_at=created_at,
            )
            if not isinstance(control.payload, ForkControlPayload):
                raise RuntimeError(f"thread fork has no anchor: {thread.id}")
            event = ThreadForked(
                thread=thread.id,
                control=control.ref,
                source_thread=source.id,
                anchor_run=str(control.payload.fork_at),
                created_at=created_at,
            )
            return thread.id, event

        return self._apply(operation)

    def rewind(
        self,
        *,
        thread_id: str,
        run_id: str | None = None,
        request_id: str | None = None,
        client_id: str | None = None,
    ) -> None:
        """Discard a terminal anchor and suffix from one idle thread."""

        created_at = utc_now()

        def operation() -> tuple[None, ThreadEvent]:
            thread = self._branchable_thread(thread_id)
            updated, control, ejected = self.store.rewind_thread(
                thread_id=thread.id,
                anchor=run_id,
                request_id=request_id,
                client_id=client_id,
                expected_head=self.store.thread_view(thread.id).head,
                created_at=created_at,
            )
            if not isinstance(control.payload, RewindControlPayload):
                raise RuntimeError(f"thread rewind has no anchor: {updated.id}")
            event = ThreadRewound(
                thread=updated.id,
                control=control.ref,
                anchor_run=str(control.payload.rewind_from),
                ejected_runs=ejected,
                created_at=created_at,
            )
            return None, event

        return self._apply(operation)

    def _branchable_thread(self, thread_id: str) -> ThreadRecord:
        thread = self.store.get_thread(thread_id=thread_id)
        if thread is None:
            raise FileNotFoundError(f"thread not found: {thread_id}")
        if thread.origin != "chat":
            raise ValueError(f"thread cannot be branched: {thread.id}")
        return thread

    def _notify(self, event: ThreadEvent) -> None:
        if self.listener is None:
            return
        try:
            self.listener.on_event(event)
        except Exception:
            _LOGGER.exception("thread listener event handling failed")
