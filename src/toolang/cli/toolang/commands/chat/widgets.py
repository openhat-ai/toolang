"""Prompt-toolkit widgets for terminal chat."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from prompt_toolkit.application import get_app
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.completion import Completer
from prompt_toolkit.filters import Condition, has_focus
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
from .input import normalize_chat_input
from . import shortcuts
from .rendering import (
    ACCENT_CELL,
    INPUT_BACKGROUND,
    RUN_CONTROL_ACCENT_PROMPT_TOOLKIT,
)

MAX_INPUT_ROWS = 6
MAX_QUEUE_ENTRIES = 8
_QUEUE_ENTRY_INSET = 1
_QUEUE_ENTRY_PADDING = 1
_QUEUE_HINT_GAP = 2
_QUEUE_MIN_PREVIEW_WIDTH = 3
_INPUT_PLACEHOLDER = "Ask or describe a task"
_STATUS_SPINNER_STYLES: dict[str, tuple[str, tuple[str, ...]]] = {
    "circles": ("■", ("◐", "◓", "◑", "◒")),
    "quadrants": (" ", ("▖", "▘", "▝", "▗")),
    "hatch": ("▦", ("▤", "▥", "▧", "▨")),
    "dots": ("⠿", ("⠾", "⠷", "⠟", "⠻")),
    "triangles": ("▪︎", ("◤", "◥", "◢", "◣")),
    "squares": ("■", ("◧", "◩", "◨", "◪")),
}
_STATUS_SPINNER_STYLE = "squares"
_STATUS_IDLE_MARKER, _STATUS_SPINNER_FRAMES = _STATUS_SPINNER_STYLES[
    _STATUS_SPINNER_STYLE
]


def _chat_ui_palette() -> dict[str, str]:
    return {
        "": "",
        "queue": "fg:#f5f5f5 bg:#3a3a3a",
        "queue.number": "dim",
        "queue.selected": f"bg:{INPUT_BACKGROUND}",
        "queue.selected.number": "dim",
        "queue.selected.hint": "fg:#d0d0d0 dim",
        "queue.info": "fg:#b8b8b8 bg:#3a3a3a dim",
        "queue.hint": "fg:#b8b8b8 dim",
        "control.run": f"bg:{RUN_CONTROL_ACCENT_PROMPT_TOOLKIT}",
        "input": f"fg:#f5f5f5 bg:{INPUT_BACKGROUND}",
        "input.placeholder": f"fg:#b8b8b8 bg:{INPUT_BACKGROUND}",
        "cursor": "fg:#111111 bg:#eeeeee",
        "input.cursor": "fg:#111111 bg:#eeeeee",
        "status": "",
        "status.marker": "dim",
        "status.spinner": f"fg:{RUN_CONTROL_ACCENT_PROMPT_TOOLKIT}",
        "status.elapsed": "dim",
        "status.error.marker": "fg:ansired",
        "status.error": "fg:ansired",
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
    def __init__(
        self,
        get_items: Callable[[], Sequence[str]],
        *,
        get_max_rows: Callable[[], int] | None = None,
    ) -> None:
        self.get_items = get_items
        self._get_max_rows = get_max_rows
        self._selected_index = 0
        self.expanded = True
        self.view = FormattedTextControl(self._render, focusable=True)
        self._has_focus = has_focus(self.view)

    def container(self) -> ConditionalContainer:
        return ConditionalContainer(
            Window(
                self.view,
                width=self.width,
                height=self.rows,
                wrap_lines=False,
                always_hide_cursor=True,
                style="class:queue",
                char=" ",
            ),
            filter=Condition(lambda: bool(self.get_items()) and self.width() > 0),
        )

    def _render(self) -> list[tuple[str, str]]:
        items = tuple(self.get_items())
        width = self.width()
        if not items or not width:
            return []
        rows = self._rows(items, width=width)
        fragments: list[tuple[str, str]] = []
        for row_index, row in enumerate(rows):
            fragments.extend(row)
            if row_index < len(rows) - 1:
                fragments.append(("", "\n"))
        return fragments

    def width(self) -> int:
        return max(0, self._terminal_width())

    def rows(self) -> int:
        count = len(self.get_items())
        width = self.width()
        if not count or not width:
            return 0
        if not self.expanded:
            return 1
        return 1 + self._entry_count(count, width) + len(self._hint_lines(width))

    def minimum_rows(self) -> int:
        """Reserve summary, one entry, and hints before sizing the input viewport."""
        width = self.width()
        if not self.get_items() or not width:
            return 0
        return 2 + len(self._hint_lines(width)) if self.expanded else 1

    def _entry_count(self, count: int, width: int) -> int:
        limit = MAX_QUEUE_ENTRIES
        if self._get_max_rows is not None:
            available = self._get_max_rows() - 1 - len(self._hint_lines(width))
            limit = min(limit, max(1, available))
        return min(count, limit)

    def toggle_expanded(self) -> bool:
        if not self.get_items():
            return False
        self.expanded = not self.expanded
        return True

    @property
    def selected_index(self) -> int | None:
        return self._selected_index if self.get_items() else None

    def move_selection(self, offset: int) -> bool:
        count = len(self.get_items())
        if not count or not self.expanded:
            return False
        selected = min(max(self._selected_index + offset, 0), count - 1)
        if selected == self._selected_index:
            return False
        self._selected_index = selected
        return True

    def reconcile(self, *, removed_index: int | None = None) -> bool:
        count = len(self.get_items())
        if not count:
            self._selected_index = 0
            self.expanded = True
            return False
        if removed_index is not None and removed_index < self._selected_index:
            self._selected_index -= 1
        self._selected_index = min(max(self._selected_index, 0), count - 1)
        return True

    def _rows(
        self,
        items: Sequence[str],
        *,
        width: int,
    ) -> list[list[tuple[str, str]]]:
        summary = self._summary_row(len(items), width=width)
        if not self.expanded:
            return [summary]
        entry_count = self._entry_count(len(items), width)
        start = min(
            max(0, self._selected_index - entry_count + 1),
            max(0, len(items) - entry_count),
        )
        focused = self._has_focus()
        rows = [
            summary,
            *(
                self._entry_row(
                    number=index + 1,
                    source=items[index],
                    width=width,
                    selected=focused and index == self._selected_index,
                )
                for index in range(start, start + entry_count)
            ),
        ]
        rows.extend(
            [("class:queue.hint", " " * (width - get_cwidth(hint)) + hint)]
            for hint in self._hint_lines(width)
        )
        return rows

    def _entry_row(
        self, *, number: int, source: str, width: int, selected: bool
    ) -> list[tuple[str, str]]:
        """Lay out one inset highlight with numbered text and trailing actions."""
        style = "class:queue.selected" if selected else "class:queue"
        # Use child styles so number/hint attributes retain the row background.
        number_style = f"{style}.number"
        hint_style = f"{style}.hint" if selected else style
        inner_width = max(0, width - 2 * _QUEUE_ENTRY_INSET)
        right_padding = " " * min(_QUEUE_ENTRY_PADDING, inner_width)
        available = inner_width - len(right_padding)
        prefix = " " * _QUEUE_ENTRY_PADDING + f"[{number}]"
        preview = " ".join(source.split())
        hint = ""
        if selected:
            actions = " · ".join(
                (
                    shortcuts.QUEUE_STEER.hint("Steer"),
                    shortcuts.QUEUE_EDIT.hint("Edit"),
                    shortcuts.QUEUE_DELETE.hint("Delete"),
                )
            )
            minimum_text = len(prefix) + 1 + _QUEUE_MIN_PREVIEW_WIDTH
            hint = self._truncate(
                actions, max(0, available - minimum_text - _QUEUE_HINT_GAP)
            )
        text_width = max(
            0, available - get_cwidth(hint) - (_QUEUE_HINT_GAP if hint else 0)
        )
        text = self._truncate(f"{prefix} {preview}", text_width)
        gap = " " * (available - get_cwidth(text) - get_cwidth(hint))
        return [
            ("class:queue", " " * min(_QUEUE_ENTRY_INSET, width)),
            (number_style, text[: len(prefix)]),
            (style, text[len(prefix) :] + gap),
            (hint_style, hint + right_padding),
            (
                "class:queue",
                " " * min(_QUEUE_ENTRY_INSET, max(0, width - _QUEUE_ENTRY_INSET)),
            ),
        ]

    def _hints(self) -> tuple[str, ...]:
        if not self._has_focus():
            return (shortcuts.SWITCH_AREA.hint("Focus"),)
        hints = (
            shortcuts.QUEUE_TOGGLE.hint("Collapse" if self.expanded else "Expand"),
            shortcuts.SWITCH_AREA.hint("Input"),
        )
        if not self.expanded:
            return hints
        return (
            f"{shortcuts.QUEUE_PREVIOUS.label}{shortcuts.QUEUE_NEXT.label} select",
            *hints,
        )

    def _hint_lines(self, width: int) -> list[str]:
        """Fit panel actions into right-aligned rows below the entries."""
        available = max(0, width - 2)
        if not available:
            return [""]
        lines: list[str] = []
        current = ""
        for hint in self._hints():
            hint = self._truncate(hint, available)
            combined = f"{current} · {hint}" if current else hint
            if get_cwidth(combined) > available:
                lines.append(current)
                current = hint
            else:
                current = combined
        return [*lines, current]

    def _summary_row(self, count: int, *, width: int) -> list[tuple[str, str]]:
        """Center on the full panel, reserving only the remaining right margin."""
        summary = self._truncate(self._count_label(count), max(0, width - 2))
        summary_width = get_cwidth(summary)
        left = max(0, (width - summary_width) // 2)
        right = width - left - summary_width
        hint = ""
        if not self.expanded:
            for action in self._hints():
                combined = f"{hint} · {action}" if hint else action
                if get_cwidth(combined) > right - 2:
                    break
                hint = combined
        return [
            (
                "class:queue" if self._has_focus() else "class:queue.info",
                " " * left + summary + " " * (right - get_cwidth(hint)),
            ),
            ("class:queue.hint", hint),
        ]

    @staticmethod
    def _count_label(count: int) -> str:
        return f"{count} item{'' if count == 1 else 's'} queued"

    @staticmethod
    def _truncate(text: str, width: int) -> str:
        """Use the same cell accounting as Prompt Toolkit's renderer."""
        if get_cwidth(text) <= width:
            return text
        if width <= 0:
            return ""
        remaining = width - 1
        for index, char in enumerate(text):
            remaining -= get_cwidth(char)
            if remaining < 0:
                return text[:index].rstrip() + "…"
        return text

    @staticmethod
    def _terminal_width() -> int:
        return get_app().output.get_size().columns


class PromptBox:
    def __init__(
        self,
        emit: Callable[[ChatUIEvent], None],
        invalidate: Callable[[], None],
        *,
        on_input: Callable[[], None] | None = None,
        history_store: ChatInputHistoryStore | None = None,
        completer: Completer | None = None,
        get_max_rows: Callable[[], int] | None = None,
    ) -> None:
        self.emit = emit
        self.invalidate = invalidate
        self.on_input = on_input
        self._get_max_rows = get_max_rows
        self.history = InMemoryHistory()
        self.history_store = history_store
        for entry in history_store.load() if history_store is not None else ():
            self.history.append_string(entry)
        self.buffer = Buffer(
            multiline=True,
            history=self.history,
            completer=completer,
            complete_while_typing=completer is not None,
        )
        self.history_index: int | None = None
        self.history_draft = ""
        self.buffer.on_text_changed += self._handle_text_changed
        self.buffer.on_cursor_position_changed += self._handle_cursor_position_changed

    def container(self) -> VSplit:
        content = HSplit(
            [
                Window(
                    height=1,
                    style="class:input",
                    always_hide_cursor=True,
                    char=" ",
                    wrap_lines=False,
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
                    style="class:control.run",
                    always_hide_cursor=True,
                    char=ACCENT_CELL,
                ),
                content,
            ],
            height=self._height_dimension,
            style="class:input",
        )

    def bind(self, keys: KeyBindings) -> None:
        def submit(_event) -> None:
            message = normalize_chat_input(self.buffer.text)
            if not message:
                return
            self._notify_input()
            self.emit(ChatUIEvent("submit", message))
            self.invalidate()

        def steer(_event) -> None:
            message = normalize_chat_input(self.buffer.text)
            if not message:
                return
            self._notify_input()
            self.emit(ChatUIEvent("steer", message))
            self.invalidate()

        def interrupt(_event) -> None:
            self.emit(ChatUIEvent("interrupt"))

        def eof(_event) -> None:
            self._notify_input()
            self.emit(ChatUIEvent("eof"))

        def quit_app(_event) -> None:
            self.emit(ChatUIEvent("quit"))

        def clear_screen(_event) -> None:
            self._notify_input()
            self.emit(ChatUIEvent("clear"))

        def insert_newline(_event) -> None:
            self._insert_newline()

        def dismiss_status_error(_event) -> None:
            self._notify_input()

        def cancel_run(_event) -> None:
            self._notify_input()
            self.emit(ChatUIEvent("cancel"))

        def previous_history(_event) -> None:
            self._notify_input()
            self._previous_history()

        def next_history(_event) -> None:
            self._notify_input()
            self._next_history()

        prompt_bindings = (
            (shortcuts.SUBMIT, submit),
            (shortcuts.STEER, steer),
            (shortcuts.INSERT_NEWLINE, insert_newline),
            (shortcuts.PREVIOUS_HISTORY, previous_history),
            (shortcuts.NEXT_HISTORY, next_history),
            (shortcuts.INTERRUPT, interrupt),
            (shortcuts.EOF, eof),
        )
        prompt_focus = has_focus(self.buffer)
        for shortcut, handler in prompt_bindings:
            for binding in shortcut.bindings:
                keys.add(*binding, filter=prompt_focus)(handler)
        global_bindings = (
            (shortcuts.QUIT, quit_app),
            (shortcuts.CLEAR, clear_screen),
            (shortcuts.DISMISS_STATUS, dismiss_status_error),
        )
        for shortcut, handler in global_bindings:
            for binding in shortcut.bindings:
                keys.add(*binding)(handler)
        for binding in shortcuts.CANCEL_RUN.bindings:
            keys.add(*binding, filter=prompt_focus, eager=True)(cancel_run)
        for binding in shortcuts.INSERT_NEWLINE.optional_bindings:
            try:
                keys.add(*binding, filter=prompt_focus)(insert_newline)
            except ValueError:
                pass

    def accept_submission(self, message: str) -> None:
        """Record accepted input and clear it if the draft has not changed."""

        self._record_history(message)
        self.history_index = None
        self.history_draft = ""
        if normalize_chat_input(self.buffer.text) == message:
            self.buffer.text = ""
        self.invalidate()

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
        self._notify_input()
        if self.history_index is None:
            return
        entries = self._history_entries()
        if self.buffer.text != entries[self.history_index]:
            self.history_index = None
            self.history_draft = ""

    def _handle_cursor_position_changed(self, _buffer: Buffer) -> None:
        self._notify_input()

    def _notify_input(self) -> None:
        if self.on_input is not None:
            self.on_input()

    def _input_rows(self) -> int:
        terminal_width = get_app().output.get_size().columns
        input_width = max(1, terminal_width - 3)
        # BufferControl reserves one trailing cursor cell per logical line.
        rows = sum(
            max(1, (get_cwidth(line) + input_width) // input_width)
            for line in self.buffer.document.lines
        )
        limit = MAX_INPUT_ROWS
        if self._get_max_rows is not None:
            limit = min(limit, max(1, self._get_max_rows() - 2))
        return min(limit, rows)

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
        self._transient_error = ""
        self._persistent_error = ""
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

    @property
    def error_message(self) -> str:
        return self._persistent_error or self._transient_error

    def set_error(self, message: str, *, persistent: bool = False) -> None:
        if persistent:
            self._persistent_error = message
        else:
            self._transient_error = message

    def clear_transient_error(self) -> bool:
        if not self._transient_error:
            return False
        self._transient_error = ""
        return True

    def clear_persistent_error(self) -> None:
        self._persistent_error = ""

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
            marker = "!"
            terminal_width = self._terminal_width()
            remaining_width = max(0, terminal_width - get_cwidth(marker))
            detail = " ".join(self.error_message.split())
            message = (
                f" {truncate(detail, remaining_width - 1)}" if remaining_width else ""
            )
            padding = " " * max(
                0,
                terminal_width - get_cwidth(f"{marker}{message}"),
            )
            return [
                ("class:status.error.marker", marker),
                ("class:status.error", message),
                ("class:status", padding),
            ]
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
        model_label = _truncate_model_label(self.model_label, fitted_model_width)
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
    def _terminal_width() -> int:
        return get_app().output.get_size().columns


def _reduce_status_width(width: int, overflow: int) -> tuple[int, int]:
    reduction = min(max(width - 1, 0), overflow)
    return width - reduction, overflow - reduction


def _truncate_model_label(label: str, width: int) -> str:
    if get_cwidth(label) <= width or " · " not in label:
        return truncate(label, width)
    model, separator, effort = label.rpartition(" · ")
    suffix = f"{separator}{effort}"
    suffix_width = get_cwidth(suffix)
    if suffix_width >= width:
        return truncate(label, width)
    return f"{truncate(model, width - suffix_width)}{suffix}"
