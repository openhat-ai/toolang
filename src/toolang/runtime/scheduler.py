"""Minimal runtime scheduler for thread-scoped serialization."""

from __future__ import annotations

import threading
from typing import Callable, TypeVar

from .requests import TurnRequest

T = TypeVar("T")


class RuntimeScheduler:
    """Serialize work by thread id while keeping the initial skeleton small."""

    def __init__(self) -> None:
        self._locks: dict[str, threading.Lock] = {}
        self._locks_lock = threading.Lock()

    def submit(self, request: TurnRequest, handler: Callable[[], T]) -> T:
        """Run one handler under the scheduler policy for its thread."""

        if request.thread_id is None:
            return handler()
        lock = self._lock_for_thread(request.thread_id)
        with lock:
            return handler()

    def _lock_for_thread(self, thread_id: str) -> threading.Lock:
        with self._locks_lock:
            lock = self._locks.get(thread_id)
            if lock is None:
                lock = threading.Lock()
                self._locks[thread_id] = lock
            return lock
