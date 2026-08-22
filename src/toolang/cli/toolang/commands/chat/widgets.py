"""Prompt-toolkit widgets for terminal chat."""

from __future__ import annotations

from collections.abc import Callable, Sequence
import shutil
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.filters import Condition
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, VSplit, Window
from prompt_toolkit.layout.containers import ConditionalContainer
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.processors import AfterInput, ConditionalProcessor
from prompt_toolkit.utils import get_cwidth

from toolang.cli.common.execution_progress.formatting import truncate

from .events import ChatUIEvent
from .history import ChatInputHistoryStore
from .rendering import (
    ACCENT_CELL,
    INPUT_BACKGROUND,
    START_CONTROL_ACCENT_PROMPT_TOOLKIT,
)

MAX_INPUT_ROWS = 6
MAX_QUEUE_ROWS = 4
_INPUT_PLACEHOLDER = "Ask anything"
_STATUS_SPINNER_STYLES: dict[str, tuple[str, tuple[str, ...]]] = {
    "circles": ("■", ("◐", "◓", "◑", "◒")),
    "quadrants": (" ", ("▖", "▘", "▝", "▗")),
    "hatch": ("▦", ("▤", "▥", "▧", "▨")),
    "dots": ("⠿", ("⠾", "⠷", "⠟", "⠻")),
    "triangles": ("▪︎", ("◤", "◥", "◢", "◣")),
    "squares": ("■", ("◧", "◩", "◨", "◪")),
}
_STATUS_SPINNER_STYLE = "circles"
_STATUS_IDLE_MARKER, _STATUS_SPINNER_FRAMES = _STATUS_SPINNER_STYLES[
    _STATUS_SPINNER_STYLE
]


def _chat_ui_palette() -> dict[str, str]:
    return {
        "": "",
        "queue": "fg:#f2f2f2 bg:#3a3a3a",
        "queue.dim": "fg:#b8b8b8 bg:#3a3a3a",
        "control.start": f"bg:{START_CONTROL_ACCENT_PROMPT_TOOLKIT}",
        "input": f"fg:#f5f5f5 bg:{INPUT_BACKGROUND}",
        "input.placeholder": f"fg:#b8b8b8 bg:{INPUT_BACKGROUND}",
        "cursor": "fg:#111111 bg:#eeeeee",
        "input.cursor": "fg:#111111 bg:#eeeeee",
        "status": "",
        "status.marker": f"fg:{INPUT_BACKGROUND}",
        "status.spinner": "",
        "status.elapsed": "dim",
        "status.error": "fg:#ffffff bg:#7a2e2e bold",
        "dim": "fg:ansigray",
    }


def _format_elapsed_seconds(seconds: int) -> str:
    seconds = max(0, seconds)
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes:02d}m {seconds:02d}s"
    if minutes:
        return f"{minutes}m {seconds:02d}s"
    return f"{seconds}s"


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

    def container(self) -> VSplit:
        content = HSplit(
            [
                Window(
                    height=1, style="class:input", always_hide_cursor=True, char=" "
                ),
                VSplit(
                    [
                        Window(
                            width=1,
                            style="class:input",
                            always_hide_cursor=True,
                            char=" ",
                        ),
                        Window(
                            BufferControl(
                                buffer=self.buffer,
                                input_processors=[
                                    ConditionalProcessor(
                                        AfterInput(
                                            _INPUT_PLACEHOLDER,
                                            style="class:input.placeholder",
                                        ),
                                        filter=Condition(lambda: not self.buffer.text),
                                    )
                                ],
                            ),
                            height=self._input_rows,
                            wrap_lines=True,
                            style="class:input",
                            char=" ",
                        ),
                        Window(
                            width=1,
                            style="class:input",
                            always_hide_cursor=True,
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
        return VSplit(
            [
                Window(
                    width=1,
                    style="class:control.start",
                    always_hide_cursor=True,
                    char=ACCENT_CELL,
                ),
                content,
            ],
            height=self._height_dimension,
            style="class:input",
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
        terminal_width = shutil.get_terminal_size((100, 24)).columns
        input_width = max(1, terminal_width - 3)
        # BufferControl reserves one trailing cursor cell per logical line.
        rows = sum(
            max(1, (get_cwidth(line) + input_width) // input_width)
            for line in self.buffer.document.lines
        )
        return min(MAX_INPUT_ROWS, rows)

    def _height_dimension(self) -> Dimension:
        rows = self.rows()
        return Dimension(min=rows, preferred=rows, max=rows, weight=0)

    def rows(self) -> int:
        """Return the fixed number of rows currently reserved for input."""

        return self._input_rows() + 2


class StatusBar:
    def __init__(self, runnable_label: str, model_label: str) -> None:
        self.runnable_label = runnable_label
        self.model_label = model_label
        self.active_runnable_label: str | None = None
        self.error_message = ""
        self.running = False
        self._spinner_index = 0
        self._elapsed_seconds = 0
        self.view = FormattedTextControl(self._render)

    def container(self) -> Window:
        return Window(
            self.view,
            height=1,
            style="class:status",
            always_hide_cursor=True,
            char=" ",
        )

    def set_status(self, runnable_label: str, model_label: str) -> None:
        self.runnable_label = runnable_label
        self.model_label = model_label

    def set_active_runnable(self, runnable_label: str | None) -> None:
        self.active_runnable_label = runnable_label

    def set_error(self, message: str) -> None:
        self.error_message = message

    def clear_error(self) -> None:
        self.error_message = ""

    def set_running(self, running: bool) -> None:
        self.running = running
        if not running:
            self.active_runnable_label = None
        self._spinner_index = 0
        self._elapsed_seconds = 0

    @property
    def spinner_index(self) -> int:
        return self._spinner_index

    @property
    def elapsed_seconds(self) -> int:
        return self._elapsed_seconds

    def set_activity(self, spinner_index: int, elapsed_seconds: int) -> bool:
        spinner_index %= len(_STATUS_SPINNER_FRAMES)
        elapsed_seconds = max(0, elapsed_seconds)
        if (
            spinner_index == self._spinner_index
            and elapsed_seconds == self._elapsed_seconds
        ):
            return False
        self._spinner_index = spinner_index
        self._elapsed_seconds = elapsed_seconds
        return True

    def _render(self) -> list[tuple[str, str]]:
        if self.error_message:
            text = f"! {self.error_message}"
            padding = " " * max(0, self._terminal_width() - get_cwidth(text))
            return [("class:status.error", f"{text}{padding}")]
        marker = (
            _STATUS_SPINNER_FRAMES[self._spinner_index]
            if self.running
            else _STATUS_IDLE_MARKER
        )
        marker_style = "class:status.spinner" if self.running else "class:status.marker"
        displayed_runnable = (
            self.active_runnable_label or self.runnable_label
            if self.running
            else self.runnable_label
        )
        default_runnable = (
            self.runnable_label
            if self.running and displayed_runnable != self.runnable_label
            else None
        )
        activity_label = (
            _format_elapsed_seconds(self._elapsed_seconds)
            if self._elapsed_seconds >= 1
            else "running"
        )
        terminal_width = self._terminal_width()
        runnable_width = get_cwidth(displayed_runnable)
        default_width = get_cwidth(default_runnable or "")
        model_width = get_cwidth(self.model_label)
        activity_width = 2 + (1 + get_cwidth(activity_label) if self.running else 0)
        fixed_width = activity_width + 1 + (3 if default_runnable else 0)
        overflow = max(
            0,
            fixed_width + runnable_width + default_width + model_width - terminal_width,
        )
        fitted_default_width, overflow = _reduce_status_width(default_width, overflow)
        fitted_runnable_width, overflow = _reduce_status_width(runnable_width, overflow)
        fitted_model_width, _overflow = _reduce_status_width(model_width, overflow)
        displayed_runnable = truncate(displayed_runnable, fitted_runnable_width)
        default_runnable = (
            truncate(default_runnable, fitted_default_width)
            if default_runnable is not None
            else None
        )
        model_label = truncate(self.model_label, fitted_model_width)
        segments = [
            (marker_style, marker),
            ("class:status", " "),
            ("class:status", displayed_runnable),
        ]
        if self.running:
            segments.append(("class:status.elapsed", f" {activity_label}"))
        used = sum(get_cwidth(text) for _style, text in segments)
        right_width = get_cwidth(model_label) + (
            get_cwidth(default_runnable) + 3 if default_runnable is not None else 0
        )
        padding = max(
            1,
            terminal_width - used - right_width,
        )
        result = [
            *segments,
            ("class:status", " " * padding),
        ]
        if default_runnable is not None:
            result.extend(
                [
                    ("class:status", default_runnable),
                    ("class:status", " · "),
                ]
            )
        result.append(("class:status", model_label))
        return result

    @staticmethod
    def _terminal_width(default: int = 100) -> int:
        return shutil.get_terminal_size((default, 24)).columns


def _reduce_status_width(width: int, overflow: int) -> tuple[int, int]:
    reduction = min(max(width - 1, 0), overflow)
    return width - reduction, overflow - reduction
