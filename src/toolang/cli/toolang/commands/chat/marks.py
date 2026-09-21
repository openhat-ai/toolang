"""Keep tmux identity marks in sync with one chat session."""

from __future__ import annotations

from collections.abc import Callable
from queue import Queue
from threading import Event, Thread

from toolang.cli.common.tmux import MARK_PAD, MARK_THREAD, PAD_CHAT, Marks


class ChatMarks:
    """Publish identity in order without blocking the TUI event loop.

    The UI starts one daemon worker before publication. Exit cancels queued
    writes and cleans up the pad after the in-flight operation, with a bounded
    wait. Synchronous operation is available before the UI loop starts.
    """

    def __init__(self, *, marks: Marks | None) -> None:
        self.marks = marks
        self._thread_id: str | None = None
        self._pending: Queue[Callable[[], None] | None] = Queue()
        self._worker: Thread | None = None
        self._closed = Event()

    @classmethod
    def disabled(cls) -> ChatMarks:
        return cls(marks=None)

    @property
    def active(self) -> bool:
        return self.marks is not None

    @property
    def thread_id(self) -> str | None:
        return self._thread_id

    def start_background(self) -> None:
        """Keep interactive metadata writes off the UI event loop."""

        if self.marks is None or self._worker is not None or self._closed.is_set():
            return
        self._worker = Thread(target=self._run, name="chat-tmux-marks", daemon=True)
        self._worker.start()

    def start(self, thread_id: str | None) -> None:
        if self.marks is None or self._closed.is_set():
            return
        marks = self.marks
        self._write(lambda: marks.set(MARK_PAD, PAD_CHAT))
        if thread_id:
            self.set_thread(thread_id)

    def set_thread(self, thread_id: str) -> None:
        if self.marks is None or self._closed.is_set() or thread_id == self._thread_id:
            return
        self._thread_id = thread_id
        marks = self.marks

        def publish() -> None:
            marks.set(MARK_THREAD, thread_id)
            if not self._closed.is_set():
                marks.name_window(thread_id)

        self._write(publish)

    def clear(self) -> None:
        """Keep container identity but remove this pane's role on exit."""

        if self._closed.is_set():
            return
        self._closed.set()
        self._thread_id = None
        if self._worker is not None:
            self._pending.put(None)
            self._worker.join(timeout=0.2)
        elif self.marks is not None:
            self.marks.clear(MARK_PAD)

    def _write(self, operation: Callable[[], None]) -> None:
        if self._worker is None:
            operation()
        else:
            self._pending.put(operation)

    def _run(self) -> None:
        try:
            while (operation := self._pending.get()) is not None:
                if not self._closed.is_set():
                    operation()
        finally:
            if self.marks is not None:
                self.marks.clear(MARK_PAD)
