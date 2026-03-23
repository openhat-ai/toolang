"""Runtime scheduler with thread serialization and coarse group budgets."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
import threading
from typing import TypeVar, cast

from .requests import TurnRequest, TurnRequestKind

T = TypeVar("T")

DEFAULT_GROUP_LIMITS: dict[TurnRequestKind, int] = {
    "invoke": 1,
    "chat": 4,
    "task": 2,
    "chore": 1,
    "will": 1,
}
DEFAULT_MAX_WORKERS = max(DEFAULT_GROUP_LIMITS.values())


class RuntimeScheduler:
    """Run turn handlers under thread and group constraints."""

    def __init__(
        self,
        *,
        max_workers: int = DEFAULT_MAX_WORKERS,
        group_limits: dict[TurnRequestKind, int] | None = None,
    ) -> None:
        self._max_workers = max_workers
        self._group_limits = dict(group_limits or DEFAULT_GROUP_LIMITS)
        self._locks: dict[str, threading.Lock] = {}
        self._locks_lock = threading.Lock()
        self._group_in_flight = {kind: 0 for kind in self._group_limits}
        self._group_in_flight_lock = threading.Lock()
        self._group_semaphores = {
            kind: threading.Semaphore(limit)
            for kind, limit in self._group_limits.items()
        }
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="toolang-runtime",
        )

    def submit(self, request: TurnRequest, handler: Callable[[], T]) -> T:
        """Submit one handler and wait for completion."""

        return self.submit_async(request, handler).result()

    def submit_async(self, request: TurnRequest, handler: Callable[[], T]) -> Future[T]:
        """Submit one handler for asynchronous execution."""

        return cast(
            Future[T], self._executor.submit(self._run_locked, request, handler)
        )

    def close(self) -> None:
        """Stop accepting work and wait for in-flight handlers."""

        self._executor.shutdown(wait=True, cancel_futures=False)

    def snapshot(self) -> dict[str, object]:
        """Return a lightweight operational snapshot of scheduler state."""

        with self._locks_lock:
            tracked_threads = len(self._locks)
        with self._group_in_flight_lock:
            thread_groups = [
                {
                    "kind": kind,
                    "limit": self._group_limits[kind],
                    "in_flight": self._group_in_flight[kind],
                    "available": max(
                        self._group_limits[kind] - self._group_in_flight[kind], 0
                    ),
                }
                for kind in self._group_limits
            ]
        return {
            "max_workers": self._max_workers,
            "tracked_threads": tracked_threads,
            "thread_groups": thread_groups,
        }

    def _run_locked(self, request: TurnRequest, handler: Callable[[], T]) -> T:
        semaphore = self._group_semaphores[request.kind]
        with semaphore:
            self._mark_group_started(request.kind)
            try:
                if request.thread_id is None:
                    return handler()
                lock = self._lock_for_thread(request.thread_id)
                with lock:
                    return handler()
            finally:
                self._mark_group_finished(request.kind)

    def _lock_for_thread(self, thread_id: str) -> threading.Lock:
        with self._locks_lock:
            lock = self._locks.get(thread_id)
            if lock is None:
                lock = threading.Lock()
                self._locks[thread_id] = lock
            return lock

    def _mark_group_started(self, kind: TurnRequestKind) -> None:
        with self._group_in_flight_lock:
            self._group_in_flight[kind] += 1

    def _mark_group_finished(self, kind: TurnRequestKind) -> None:
        with self._group_in_flight_lock:
            current = self._group_in_flight[kind]
            self._group_in_flight[kind] = max(current - 1, 0)
