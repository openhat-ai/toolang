"""Prompt-toolkit widgets for terminal chat."""

from __future__ import annotations

from collections.abc import Callable, Sequence
import shutil
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, VSplit, Window
from prompt_toolkit.layout.containers import ConditionalContainer
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.utils import get_cwidth

from .events import ChatUIEvent
from .history import ChatInputHistoryStore

MAX_INPUT_ROWS = 6
MAX_QUEUE_ROWS = 4

PROMPT_MARKER = FormattedText([("class:normal-input.dim", "> ")])


def _chat_ui_palette() -> dict[str, str]:
    return {
        "": "",
        "queue": "fg:#f2f2f2 bg:#3a3a3a",
        "queue.dim": "fg:#b8b8b8 bg:#3a3a3a",
        "normal-input": "fg:#f5f5f5 bg:#444444",
        "normal-input.dim": "fg:#b8b8b8 bg:#444444",
        "input": "fg:#f5f5f5 bg:#444444",
        "steer-input": "fg:#f5f5f5 bg:#2f555d",
        "steer-input.dim": "fg:#b8b8b8 bg:#2f555d",
        "cursor": "fg:#111111 bg:#eeeeee",
        "input.cursor": "fg:#111111 bg:#eeeeee",
        "status": "fg:#f2f2f2 bg:#5a5a5a",
        "status.model": "fg:#ffd866",
        "status.agic": "fg:#8fd7ff",
        "status.flow": "fg:#d7b3ff",
        "status.text": "fg:ansigray",
        "status.error": "fg:#ffffff bg:#7a2e2e bold",
        "dim": "fg:ansigray",
    }


class QueuePanel:
    def __init__(self, get_items: Callable[[], Sequence[str]]) -> None:
        self.get_items = get_items
        self.view = FormattedTextControl(self._render)

    def container(self) -> ConditionalContainer:
        return ConditionalContainer(
            Window(
                self.view,
                height=self.rows,
                wrap_lines=False,
                always_hide_cursor=True,
                style="class:queue",
                char=" ",
            ),
            filter=Condition(lambda: bool(self.get_items())),
        )

    def _render(self) -> list[tuple[str, str]]:
        items = list(enumerate(self.get_items(), 1))
        shown = items[:MAX_QUEUE_ROWS]
        hidden = len(items) - len(shown)
        suffix = f" ({hidden} more not shown)" if hidden else ""
        rows: list[list[tuple[str, str]]] = [
            [("class:queue.dim", f"  queued for submission:{suffix}")]
        ]
        rows.extend(
            [
                ("class:queue", "  "),
                ("class:queue.dim", f"[{index}]"),
                ("class:queue", f" {self._summarize(item)}"),
            ]
            for index, item in shown
        )

        fragments: list[tuple[str, str]] = []
        width = self._terminal_width()
        for row_index, row in enumerate(rows):
            visible_len = 0
            for style, text in row:
                fragments.append((style, text))
                visible_len += get_cwidth(text)
            if padding := " " * max(0, width - visible_len):
                fragments.append(("class:queue", padding))
            if row_index < len(rows) - 1:
                fragments.append(("", "\n"))
        return fragments

    def rows(self) -> int:
        return 1 + min(len(self.get_items()), MAX_QUEUE_ROWS) if self.get_items() else 0

    @staticmethod
    def _summarize(message: str, *, width: int = 72) -> str:
        text = " ".join(message.split())
        return text if len(text) <= width else f"{text[: width - 3].rstrip()}..."

    @staticmethod
    def _terminal_width(default: int = 100) -> int:
        import shutil

        return shutil.get_terminal_size((default, 24)).columns


class PromptBox:
    def __init__(
        self,
        emit: Callable[[ChatUIEvent], None],
        invalidate: Callable[[], None],
        *,
        on_text_changed: Callable[[], None] | None = None,
        history_store: ChatInputHistoryStore | None = None,
    ) -> None:
        self.emit = emit
        self.invalidate = invalidate
        self.on_text_changed = on_text_changed
        self.history = InMemoryHistory()
        self.history_store = history_store
        for entry in history_store.load() if history_store is not None else ():
            self.history.append_string(entry)
        self.buffer = Buffer(multiline=True, history=self.history)
        self.history_index: int | None = None
        self.history_draft = ""
        self.buffer.on_text_changed += self._handle_text_changed

    def container(self) -> HSplit:
        return HSplit(
            [
                Window(
                    height=1, style="class:input", always_hide_cursor=True, char=" "
                ),
                VSplit(
                    [
                        self._marker_window(),
                        Window(
                            BufferControl(buffer=self.buffer),
                            height=self._input_rows,
                            wrap_lines=True,
                            style="class:input",
                            char=" ",
                        ),
                    ],
                    height=self._input_rows,
                    style="class:input",
                ),
                Window(
                    height=1, style="class:input", always_hide_cursor=True, char=" "
                ),
            ],
            height=self._height_dimension,
        )

    def _marker_window(self) -> Window:
        return Window(
            FormattedTextControl(PROMPT_MARKER),
            width=2,
            style="class:input",
            char=" ",
        )

    def bind(self, keys: KeyBindings) -> None:
        @keys.add("enter")
        def submit(_event) -> None:
            message = self.buffer.text.strip()
            if not message:
                return
            self._record_history(message)
            self.buffer.text = ""
            self.history_index = None
            self.history_draft = ""
            self.emit(ChatUIEvent("submit", message))
            self.invalidate()

        @keys.add("c-c")
        def interrupt(_event) -> None:
            self.emit(ChatUIEvent("interrupt"))

        @keys.add("c-d")
        def eof(_event) -> None:
            self.emit(ChatUIEvent("eof"))

        @keys.add("c-q")
        def quit_app(_event) -> None:
            self.emit(ChatUIEvent("quit"))

        @keys.add("c-l")
        def clear_screen(_event) -> None:
            self.emit(ChatUIEvent("clear"))

        @keys.add("c-j")
        @keys.add("escape", "enter")
        def insert_newline(_event) -> None:
            self._insert_newline()

        @keys.add("escape", "escape", eager=True)
        def cancel_run(_event) -> None:
            self.emit(ChatUIEvent("cancel"))

        @keys.add("up")
        @keys.add("c-p")
        def previous_history(_event) -> None:
            self._previous_history()

        @keys.add("down")
        @keys.add("c-n")
        def next_history(_event) -> None:
            self._next_history()

        try:
            keys.add("s-enter")(lambda _event: self._insert_newline())
        except ValueError:
            pass

    def _insert_newline(self) -> None:
        self.buffer.insert_text("\n")
        self.invalidate()

    def has_input(self) -> bool:
        return bool(self.buffer.text)

    def clear_input(self) -> None:
        if not self.buffer.text:
            return
        self.buffer.text = ""
        self.history_index = None
        self.history_draft = ""
        self.invalidate()

    def _record_history(self, message: str) -> None:
        entries = self._history_entries()
        if entries and entries[-1] == message:
            return
        self.history.append_string(message)
        if self.history_store is None:
            return
        try:
            self.history_store.append(message)
        except OSError:
            pass

    def _previous_history(self) -> None:
        if self.buffer.document.cursor_position_row > 0:
            self.buffer.cursor_up()
            return
        entries = self._history_entries()
        if not entries:
            return
        if self.history_index is None:
            self.history_draft = self.buffer.text
            self.history_index = len(entries) - 1
        else:
            self.history_index = max(0, self.history_index - 1)
        self.replace_input(entries[self.history_index])

    def _next_history(self) -> None:
        if (
            self.buffer.document.cursor_position_row
            < self.buffer.document.line_count - 1
        ):
            self.buffer.cursor_down()
            return
        if self.history_index is None:
            return
        entries = self._history_entries()
        if self.history_index < len(entries) - 1:
            self.history_index += 1
            self.replace_input(entries[self.history_index])
        else:
            self.history_index = None
            self.replace_input(self.history_draft)
            self.history_draft = ""

    def _history_entries(self) -> list[str]:
        return list(self.history.get_strings())

    def replace_input(self, text: str) -> None:
        self.buffer.text = text
        self.buffer.cursor_position = len(text)
        self.invalidate()

    def _handle_text_changed(self, _buffer: Buffer) -> None:
        if self.on_text_changed is not None:
            self.on_text_changed()
        if self.history_index is None:
            return
        entries = self._history_entries()
        if self.buffer.text != entries[self.history_index]:
            self.history_index = None
            self.history_draft = ""

    def _input_rows(self) -> int:
        return min(MAX_INPUT_ROWS, max(1, self.buffer.document.line_count))

    def _height_dimension(self) -> Dimension:
        rows = self.rows()
        return Dimension(min=rows, preferred=rows, max=rows, weight=0)

    def rows(self) -> int:
        """Return the fixed number of rows currently reserved for input."""

        return self._input_rows() + 2


class StatusBar:
    def __init__(self, status_label: str) -> None:
        self.status_label = status_label
        self.error_message = ""
        self.view = FormattedTextControl(self._render)

    def container(self) -> Window:
        return Window(
            self.view,
            height=1,
            style="class:status",
            always_hide_cursor=True,
            char=" ",
        )

    def set_status(self, status_label: str) -> None:
        self.status_label = status_label

    def set_error(self, message: str) -> None:
        self.error_message = message

    def clear_error(self) -> None:
        self.error_message = ""

    def _render(self) -> list[tuple[str, str]]:
        if self.error_message:
            text = f"! {self.error_message}"
            padding = " " * max(0, self._terminal_width() - get_cwidth(text))
            return [("class:status.error", f"{text}{padding}")]
        pieces = [piece for piece in self.status_label.split("  ") if piece]
        segments: list[tuple[str, str]] = []
        if pieces:
            segments.append(("class:status.model", f"  {pieces[0]}"))
        for piece in pieces[1:]:
            if piece.startswith("agic:"):
                segments.extend(
                    [("class:status.text", "  "), ("class:status.agic", piece)]
                )
            elif piece.startswith("flow:"):
                segments.extend(
                    [("class:status.text", "  "), ("class:status.flow", piece)]
                )
            else:
                segments.append(("class:status.text", f"  {piece}"))
        shortcuts = "  ^d exit  ^j newline  ↑↓ history  "
        used = sum(get_cwidth(text) for _style, text in segments)
        padding = max(2, self._terminal_width() - used - get_cwidth(shortcuts))
        return [
            *segments,
            ("class:status.text", " " * padding),
            ("class:status.text", shortcuts),
        ]

    @staticmethod
    def _terminal_width(default: int = 100) -> int:
        return shutil.get_terminal_size((default, 24)).columns
