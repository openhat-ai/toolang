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

from toolang.common.time import format_duration
from toolang.cli.common.execution_progress.formatting import truncate
from toolang.lang.types import display_runnable_ref
from toolang.cli.common.terminal_surfaces import (
    DARK_TERMINAL_SURFACES,
    TerminalSurfaces,
)
from .events import ChatUIEvent
from .history import ChatInputHistoryStore
from .input import normalize_chat_input
from . import shortcuts
from .rendering import (
    ACCENT_CELL,
    RUN_CONTROL_ACCENT_PROMPT_TOOLKIT,
    STEER_CONTROL_ACCENT_PROMPT_TOOLKIT,
)

MAX_INPUT_ROWS = 6
MAX_QUEUE_ENTRIES = 8
# Queue accents its leading cell like Input, reusing Steer's magenta.
_QUEUE_ACCENT_WIDTH = 1
# An expanded panel frames its entries with a summary and a trailing blank
# row that separates Queue from the Input box. A collapsed
# panel keeps only its summary.
_QUEUE_FRAME_ROWS = 2
# One blank cell follows the accent; two blank cells end each entry row.
_QUEUE_TEXT_INSET = 1
_QUEUE_ROW_PADDING = 2
_QUEUE_ENTRY_ICON = "↳"
_QUEUE_HINT_GAP = 2
_QUEUE_MIN_PREVIEW_WIDTH = 3
_INPUT_PLACEHOLDER = "Ask or describe a task"
# The status bar insets its content on each side so its text lines up with the
# other chat surfaces; the inset cells stay blank.
_STATUS_INSET = "  "
_STATUS_CENTER_GAP = 1


def _chat_ui_palette(
    surfaces: TerminalSurfaces = DARK_TERMINAL_SURFACES,
) -> dict[str, str]:
    return {
        "": "",
        "queue": f"bg:{surfaces.queue_background}",
        "queue.accent": f"bg:{STEER_CONTROL_ACCENT_PROMPT_TOOLKIT}",
        "queue.icon": "dim",
        "queue.selected": f"bg:{surfaces.input_background}",
        "queue.selected.icon": "dim",
        "queue.selected.hint": "dim",
        "queue.hint": "dim",
        "queue.count": "dim",
        "queue.focused-count": "bold",
        "control.run": f"bg:{RUN_CONTROL_ACCENT_PROMPT_TOOLKIT}",
        "input": f"bg:{surfaces.input_background}",
        "input.placeholder": f"bg:{surfaces.input_background} dim",
        "cursor": "reverse",
        "input.cursor": "reverse",
        "status": "",
        "status.context": "",
        "status.context.symbol": "dim",
        "status.elapsed": "dim",
        "status.error.marker": "fg:ansired",
        "status.error": "fg:ansired",
        "dim": "dim",
    }


def _chat_runnable_label(reference: str) -> str:
    """Return one Chat status label without module or source line."""

    return display_runnable_ref(reference.rsplit("::", 1)[-1], surface="chat")


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
        if not count or not self.width():
            return 0
        if not self.expanded:
            return 1
        return _QUEUE_FRAME_ROWS + self._entry_count(count)

    def minimum_rows(self) -> int:
        """Reserve the panel frame and one entry before sizing the input viewport."""
        if not self.get_items() or not self.width():
            return 0
        return _QUEUE_FRAME_ROWS + 1 if self.expanded else 1

    def _entry_count(self, count: int) -> int:
        limit = MAX_QUEUE_ENTRIES
        if self._get_max_rows is not None:
            available = self._get_max_rows() - _QUEUE_FRAME_ROWS
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

    @staticmethod
    def _accent_cell() -> tuple[str, str]:
        """Return the leading accent cell placed before queue content."""

        return ("class:queue.accent", ACCENT_CELL)

    def _blank_row(self, width: int) -> list[tuple[str, str]]:
        """Return a blank row that only carries the accent and surface."""

        return [self._accent_cell(), ("class:queue", " " * width)]

    def _title_hint(self) -> str:
        """Return the one panel action shown beside the count for this state."""

        if not self._has_focus():
            return shortcuts.SWITCH_AREA.hint_phrase("focus")
        return shortcuts.QUEUE_TOGGLE.hint_phrase(
            "collapse" if self.expanded else "expand"
        )

    def _rows(
        self,
        items: Sequence[str],
        *,
        width: int,
    ) -> list[list[tuple[str, str]]]:
        content_width = max(0, width - _QUEUE_ACCENT_WIDTH)
        rows = [
            [self._accent_cell(), *self._summary_row(len(items), width=content_width)]
        ]
        if not self.expanded:
            return rows
        entry_count = self._entry_count(len(items))
        start = min(
            max(0, self._selected_index - entry_count + 1),
            max(0, len(items) - entry_count),
        )
        focused = self._has_focus()
        rows.extend(
            [
                self._accent_cell(),
                *self._entry_row(
                    source=items[index],
                    width=content_width,
                    selected=focused and index == self._selected_index,
                ),
            ]
            for index in range(start, start + entry_count)
        )
        # Keep a blank row between Queue and the Input box below it.
        rows.append(self._blank_row(content_width))
        return rows

    def _entry_row(
        self, *, source: str, width: int, selected: bool
    ) -> list[tuple[str, str]]:
        """Lay out one entry row with a dim icon and trailing action hints."""
        style = "class:queue.selected" if selected else "class:queue"
        # Use child styles so icon/hint attributes retain the row background.
        icon_style = f"{style}.icon"
        hint_style = f"{style}.hint" if selected else style
        right_padding = " " * min(_QUEUE_ROW_PADDING, width)
        available = max(0, width - len(right_padding))
        prefix = " " * _QUEUE_TEXT_INSET + _QUEUE_ENTRY_ICON
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
        # Highlighted padding lets a selection reach Queue's right edge.
        return [
            (icon_style, text[: len(prefix)]),
            (style, text[len(prefix) :] + gap),
            (hint_style, hint + right_padding),
        ]

    def _summary_row(self, count: int, *, width: int) -> list[tuple[str, str]]:
        """Center the count independently of the right-aligned action hint."""

        style = "class:queue"
        right = min(_QUEUE_ROW_PADDING, width)
        available = width - right
        # Count text takes priority over padding when the terminal is narrow.
        label = self._truncate(self._count_label(count), width)
        label_width = get_cwidth(label)
        # Width excludes the leading accent. Center against the full panel,
        # then translate back to content coordinates and clamp narrow layouts.
        centered = (width + _QUEUE_ACCENT_WIDTH - label_width) // 2
        start = max(0, centered - _QUEUE_ACCENT_WIDTH)
        count_style = (
            "class:queue.focused-count" if self._has_focus() else "class:queue.count"
        )
        cells: list[tuple[str, str]] = [
            (style, " " * start),
            (count_style, label),
        ]
        used = start + label_width
        hint = self._title_hint()
        hint_width = get_cwidth(hint)
        if label_width and used + _QUEUE_HINT_GAP + hint_width <= available:
            cells.append((style, " " * (available - used - hint_width)))
            cells.append(("class:queue.hint", hint))
            used = available
        if used < width:
            cells.append((style, " " * (width - used)))
        return cells

    @staticmethod
    def _count_label(count: int) -> str:
        return f"{count} queued"

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
                            width=lambda: min(
                                1, max(0, get_app().output.get_size().columns - 2)
                            ),
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
                            width=lambda: min(
                                2, max(0, get_app().output.get_size().columns - 3)
                            ),
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
                    width=lambda: min(
                        1, max(0, get_app().output.get_size().columns - 1)
                    ),
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

        # Bare Up/Down stay out of input history: terminals and tmux translate
        # the mouse wheel into Up/Down in the alternate screen, and the default
        # prompt-toolkit bindings fall back to history there, so scrolling would
        # rewrite the draft. Up/Down only move the cursor inside a multi-line
        # draft; Ctrl+P/Ctrl+N remain the history keys.
        def arrow_up(_event) -> None:
            self._notify_input()
            if self.buffer.document.cursor_position_row > 0:
                self.buffer.cursor_up()

        def arrow_down(_event) -> None:
            self._notify_input()
            document = self.buffer.document
            if document.cursor_position_row < document.line_count - 1:
                self.buffer.cursor_down()

        prompt_bindings = (
            (shortcuts.SUBMIT, submit),
            (shortcuts.STEER, steer),
            (shortcuts.INSERT_NEWLINE, insert_newline),
            (shortcuts.PREVIOUS_HISTORY, previous_history),
            (shortcuts.NEXT_HISTORY, next_history),
            (shortcuts.INTERRUPT, interrupt),
        )
        prompt_focus = has_focus(self.buffer)
        for shortcut, handler in prompt_bindings:
            for binding in shortcut.bindings:
                keys.add(*binding, filter=prompt_focus)(handler)
        # Ctrl+D keeps its terminal meaning while the draft has text: the default
        # binding deletes the character after the cursor. Chat claims the key only
        # for the empty draft, where it ends input.
        empty_draft = Condition(lambda: not self.buffer.text)
        for binding in shortcuts.EOF.bindings:
            keys.add(*binding, filter=prompt_focus & empty_draft)(eof)
        keys.add("up", filter=prompt_focus)(arrow_up)
        keys.add("down", filter=prompt_focus)(arrow_down)
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
        input_width = max(1, terminal_width - 4)
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


class RunStatusBar:
    """Two-row run activity surface above Queue and Input."""

    def __init__(self, *, get_rows: Callable[[], int]) -> None:
        self.rows = get_rows
        self.running = False
        self._elapsed_seconds = 0
        self.view = FormattedTextControl(self._render)

    def container(self) -> Window:
        return Window(
            self.view,
            height=self.rows,
            style="class:status",
            always_hide_cursor=True,
            wrap_lines=False,
            char=" ",
        )

    def set_running(self, running: bool) -> None:
        self.running = running
        self._elapsed_seconds = 0

    @property
    def elapsed_seconds(self) -> int:
        return self._elapsed_seconds

    def set_elapsed_seconds(self, elapsed_seconds: int) -> bool:
        elapsed_seconds = max(0, elapsed_seconds)
        if elapsed_seconds == self._elapsed_seconds:
            return False
        self._elapsed_seconds = elapsed_seconds
        return True

    def _elapsed_label(self) -> str:
        if not self.running:
            return ""
        return (
            f"Working for {format_duration(self._elapsed_seconds)}"
            if self._elapsed_seconds >= 1
            else "Working"
        )

    def _render(self) -> list[tuple[str, str]]:
        rows = self.rows()
        if not rows:
            return []
        cells = [("class:status", "\n")] if rows == 2 else []
        width = get_app().output.get_size().columns
        inset = _STATUS_INSET if width > 2 * len(_STATUS_INSET) else ""
        label = truncate(self._elapsed_label(), max(0, width - 2 * len(inset)))
        cells.extend((("class:status", inset), ("class:status.elapsed", label)))
        return cells


class StatusBar:
    """Session settings, workspace identity, and diagnostics below Input."""

    def __init__(
        self,
        runnable_label: str,
        model_label: str,
        agent_label: str = "",
        workspace_label: str | None = None,
    ) -> None:
        self.runnable_label = runnable_label
        self.model_label = model_label
        self.agent_label = agent_label
        self.workspace_label = workspace_label
        self.run_workspace_label = workspace_label
        self._transient_error = ""
        self._persistent_error = ""
        self.running = False
        self.view = FormattedTextControl(self._render)

    def container(self) -> Window:
        return Window(
            self.view,
            height=1,
            style="class:status",
            always_hide_cursor=True,
            char=" ",
        )

    def set_status(
        self,
        runnable_label: str,
        model_label: str,
        workspace_label: str | None = None,
    ) -> None:
        self.runnable_label = runnable_label
        self.model_label = model_label
        self.workspace_label = workspace_label
        if not self.running:
            self.run_workspace_label = workspace_label

    def set_run_workspace(self, workspace_label: str | None) -> None:
        self.run_workspace_label = workspace_label

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
            self.run_workspace_label = self.workspace_label

    def _status_insets(self, *, leading: bool = True) -> tuple[str, str, int]:
        """Return the inset cells and the width left for status content.

        An error message keeps its marker in the first column, so it drops the
        leading inset. A terminal too narrow for the insets drops them rather
        than overflowing.
        """

        left_cell = _STATUS_INSET if leading else ""
        inset_width = get_cwidth(left_cell) + get_cwidth(_STATUS_INSET)
        terminal_width = self._terminal_width()
        if terminal_width > inset_width:
            return left_cell, _STATUS_INSET, terminal_width - inset_width
        return "", "", terminal_width

    def _center_label(self) -> str:
        workspace = self.run_workspace_label if self.running else self.workspace_label
        parts = [part for part in (self.agent_label, workspace) if part]
        return "@".join(parts)

    def _render(self) -> list[tuple[str, str]]:
        if self.error_message:
            marker = "!"
            left_cell, right_cell, body_width = self._status_insets(leading=False)
            remaining_width = max(0, body_width - get_cwidth(marker))
            detail = " ".join(self.error_message.split())
            message = (
                f" {truncate(detail, remaining_width - 1)}" if remaining_width else ""
            )
            padding = " " * max(
                0,
                body_width - get_cwidth(f"{marker}{message}"),
            )
            cells: list[tuple[str, str]] = (
                [("class:status", left_cell)] if left_cell else []
            )
            cells.extend(
                [
                    ("class:status.error.marker", marker),
                    ("class:status.error", message),
                    ("class:status", padding),
                ]
            )
            if right_cell:
                cells.append(("class:status", right_cell))
            return cells

        left_cell, right_cell, body_width = self._status_insets()
        full_left_label = _chat_runnable_label(self.runnable_label)
        full_model_label = self.model_label
        center_label = self._center_label()
        center_label_width = get_cwidth(center_label)

        # If even the center plus its two margins cannot fit, show it alone.
        # Only when it is wider than the whole status area do we truncate it.
        if body_width < center_label_width + 2 * _STATUS_CENTER_GAP:
            left_label = ""
            model_label = ""
            center_label = truncate(center_label, body_width)
            center_label_width = get_cwidth(center_label)
            center_start = (body_width - center_label_width) // 2
        else:
            center_start = (body_width - center_label_width) // 2
            center_end = center_start + center_label_width
            left_limit = center_start - _STATUS_CENTER_GAP
            right_limit = body_width - center_end - _STATUS_CENTER_GAP
            left_label = _truncate_status_edge_left(full_left_label, max(0, left_limit))
            model_label = _truncate_status_edge_right(
                full_model_label, max(0, right_limit)
            )

        cells: list[tuple[str, str]] = []
        if left_cell:
            cells.append(("class:status", left_cell))
        cursor = 0
        if left_label:
            cells.append(("class:status", left_label))
            cursor += get_cwidth(left_label)
        if center_label:
            if center_start > cursor:
                cells.append(("class:status", " " * (center_start - cursor)))
            if "@" in center_label:
                agent, _, workspace = center_label.partition("@")
                cells.extend(
                    (
                        ("class:status.context", agent),
                        ("class:status.context.symbol", "@"),
                        ("class:status.context", workspace),
                    )
                )
            else:
                cells.append(("class:status.context", center_label))
            cursor = center_start + center_label_width
        model_start = body_width - get_cwidth(model_label)
        if model_label:
            if model_start > cursor:
                cells.append(("class:status", " " * (model_start - cursor)))
            cells.append(("class:status", model_label))
            cursor = model_start + get_cwidth(model_label)
        if cursor < body_width:
            cells.append(("class:status", " " * (body_width - cursor)))
        if right_cell:
            cells.append(("class:status", right_cell))
        return cells

    @staticmethod
    def _terminal_width() -> int:
        return get_app().output.get_size().columns


def _truncate_status_edge_left(label: str, width: int) -> str:
    """Keep the outer (left) part of one edge label and elide inward."""

    return truncate(label, width)


def _truncate_status_edge_right(label: str, width: int) -> str:
    """Keep the outer (right) part of one edge label and elide inward."""

    if get_cwidth(label) <= width:
        return label
    if width <= 0:
        return ""
    remaining = width - 1
    suffix: list[str] = []
    for char in reversed(label):
        char_width = get_cwidth(char)
        if char_width > remaining:
            break
        suffix.append(char)
        remaining -= char_width
    return "…" + "".join(reversed(suffix))
