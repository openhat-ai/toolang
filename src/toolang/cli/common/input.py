"""Shared live input surface; each command owns its key bindings."""

from collections.abc import Callable
from prompt_toolkit.application import get_app
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.filters import Condition
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.layout import HSplit, VSplit, Window
from prompt_toolkit.layout.controls import BufferControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.processors import AfterInput, ConditionalProcessor
from prompt_toolkit.utils import get_cwidth
from .input_history import InputHistoryStore

MAX_INPUT_ROWS = 6
ACCENT_CELL = " "


class InputBox:
    def __init__(
        self,
        invalidate: Callable[[], None],
        *,
        normalize: Callable[[str], str] = str.strip,
        placeholder: str = "Write a message",
        on_input: Callable[[], None] | None = None,
        history_store: InputHistoryStore | None = None,
        get_max_rows: Callable[[], int] | None = None,
        get_width: Callable[[], int] | None = None,
    ) -> None:
        self.invalidate = invalidate
        self.normalize = normalize
        self.placeholder = placeholder
        self.on_input = on_input
        self._get_max_rows = get_max_rows
        self._get_width = get_width
        self.history = InMemoryHistory()
        self.history_store = history_store
        for entry in history_store.load() if history_store is not None else ():
            self.history.append_string(entry)
        self.buffer = Buffer(
            multiline=True,
            history=self.history,
            complete_while_typing=False,
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
                            width=lambda: min(1, max(0, self._width() - 2)),
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
                                            self.placeholder,
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
                            width=lambda: min(2, max(0, self._width() - 3)),
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
                    width=lambda: min(1, max(0, self._width() - 1)),
                    style="class:control.run",
                    always_hide_cursor=True,
                    char=ACCENT_CELL,
                ),
                content,
            ],
            height=self._height_dimension,
            style="class:input",
        )

    def accept_submission(self, message: str) -> None:
        """Record accepted input and clear it if the draft has not changed."""

        self._record_history(message)
        self.history_index = None
        self.history_draft = ""
        if self.normalize(self.buffer.text) == message:
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

    def _width(self) -> int:
        columns = get_app().output.get_size().columns
        return max(1, min(columns, self._get_width() if self._get_width else columns))

    def _input_rows(self) -> int:
        input_width = max(1, self._width() - 4)
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
