"""OSC title presentation, independent of tmux identity metadata."""

from __future__ import annotations

from collections.abc import Callable
from threading import Thread

from prompt_toolkit.output import Output
from wcwidth import wcwidth

from .base import ThreadTitle

TITLE_WIDTH = 60
ELLIPSIS = "\u2026"


def clip_title(text: str, *, width: int = TITLE_WIDTH) -> str:
    """Return one bounded display line without terminal control characters."""

    flat = "".join(
        char
        for char in " ".join(text.split())
        if ord(char) >= 32 and not 127 <= ord(char) <= 159
    )
    if width <= 0:
        return ""
    if sum(max(wcwidth(char), 0) for char in flat) <= width:
        return flat
    clipped: list[str] = []
    used = 0
    for char in flat:
        step = max(wcwidth(char), 0)
        if used + step > width - 1:
            break
        clipped.append(char)
        used += step
    return "".join(clipped).rstrip() + ELLIPSIS


class ChatTitle:
    """Look up titles off the UI loop and emit OSC only on the UI loop."""

    def __init__(
        self,
        *,
        output: Output,
        enabled: bool,
        lookup: Callable[[str], str | None],
        on_result: Callable[[ThreadTitle], None],
    ) -> None:
        self._output = output
        self._enabled = enabled
        self._lookup = lookup
        self._on_result = on_result
        self._thread_id: str | None = None
        self._generation = 0
        self._pending = False
        self._retry = False
        self._published = False
        self._written: str | None = None
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
        if not self._published:
            self._emit("[new chat]")

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

        Thread(target=lookup, name="chat-thread-title", daemon=True).start()

    def accept(self, result: ThreadTitle) -> None:
        if (
            self._closed
            or result.thread_id != self._thread_id
            or result.generation != self._generation
        ):
            return
        self._pending = False
        title = clip_title(result.title or "")
        if title:
            self._published = self._emit(title)
        retry, self._retry = self._retry, False
        if retry and not self._published:
            self.refresh()

    def clear(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._written is not None:
            self._emit("")

    def _emit(self, title: str) -> bool:
        if not self._enabled:
            return False
        title = clip_title(title)
        if title == self._written:
            return True
        try:
            # OSC 0 updates iTerm2 tabs and windows, and tmux pane titles.
            # Output.set_title() sends OSC 2, which leaves iTerm2 tabs unchanged.
            self._output.write_raw(f"\x1b]0;{title}\x07")
            self._output.flush()
        except (OSError, ValueError):
            return False
        self._written = title
        return True
