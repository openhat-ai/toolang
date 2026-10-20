"""OSC title presentation, independent of tmux identity metadata."""

from __future__ import annotations

from collections.abc import Callable
from threading import Thread

from toolang.cli.common.terminal_title import agent_label, clip_title, label_text

from .base import ThreadTitle


def chat_label(agent: str, title: str = "") -> str:
    name = clip_title(agent_label(agent), width=12)
    topic = label_text(title)
    return f"{name}> {topic}" if topic else f"{name}>"


class ChatTitle:
    """Look up titles off the UI loop and emit OSC only on the UI loop."""

    def __init__(
        self,
        *,
        publish: Callable[[str], bool],
        agent: str,
        enabled: bool,
        lookup: Callable[[str], str | None],
        on_result: Callable[[ThreadTitle], None],
    ) -> None:
        self._publish = publish
        self._agent = agent
        self._enabled = enabled
        self._lookup = lookup
        self._on_result = on_result
        self._thread_id: str | None = None
        self._generation = 0
        self._pending = False
        self._retry = False
        self._published = False
        self._closed = False

    def start(self, thread_id: str | None) -> None:
        self.set_thread(thread_id)
        if thread_id:
            self.refresh()

    def set_thread(self, thread_id: str | None) -> None:
        if not self._enabled or self._closed:
            return
        if thread_id != self._thread_id:
            self._thread_id = thread_id
            self._generation += 1
            self._pending = self._retry = self._published = False

    def refresh(self) -> None:
        if (
            not self._enabled
            or self._closed
            or self._thread_id is None
            or self._published
        ):
            return
        if self._pending:
            self._retry = True
            return
        thread_id, generation = self._thread_id, self._generation
        self._pending = True

        def lookup() -> None:
            try:
                title = self._lookup(thread_id)
            except Exception:
                title = None
            result = ThreadTitle(
                thread_id, generation, title if isinstance(title, str) else None
            )
            if not self._closed:
                self._on_result(result)

        try:
            Thread(target=lookup, name="chat-thread-title", daemon=True).start()
        except RuntimeError:
            self._pending = False

    def accept(self, result: ThreadTitle) -> None:
        if (
            self._closed
            or result.thread_id != self._thread_id
            or result.generation != self._generation
        ):
            return
        self._pending = False
        title = label_text(result.title or "")
        written = self._publish(chat_label(self._agent, title))
        self._published = written and bool(title)
        retry, self._retry = self._retry, False
        if retry and not self._published:
            self.refresh()

    def clear(self) -> None:
        if self._closed:
            return
        self._closed = True
