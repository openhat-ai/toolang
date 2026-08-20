"""Terminal output owned by the script progress renderer."""

from __future__ import annotations

import shutil
from typing import Literal, TextIO

from ..execution_progress import ProgressRow, ProgressUpdate
from ..execution_progress.config import DEFAULT_MAX_PROGRESS_WIDTH
from ..execution_progress.formatting import (
    display_width,
    one_line,
    split_hanging_prefix,
    truncate,
    wrap_display,
)

Tone = Literal["progress", "normal", "active", "error", "warning"]

_ANSI_STYLE: dict[Tone, str] = {
    "progress": "\x1b[2m",
    "normal": "",
    "active": "",
    "error": "\x1b[31m",
    "warning": "\x1b[33m",
}
_ANSI_RESET = "\x1b[0m"
_CURSOR_HIDE = "\x1b[?25l"
_CURSOR_SHOW = "\x1b[?25h"


class ProgressConsole:
    """Write finalized and replaceable progress blocks to one stream."""

    def __init__(
        self,
        stream: TextIO,
        *,
        width: int | None = None,
        max_width: int = DEFAULT_MAX_PROGRESS_WIDTH,
    ) -> None:
        self.stream = stream
        self.tty = bool(getattr(stream, "isatty", lambda: False)())
        detected = (
            shutil.get_terminal_size(fallback=(max_width, 24)).columns
            if self.tty
            else max_width
        )
        requested = detected if width is None else width
        if self.tty:
            requested = min(requested, detected)
        self.width = max(1, min(requested, max_width))
        self._live_lines: list[str] = []
        self._last_was_blank = False
        self._cursor_hidden = False

    def close(self) -> None:
        self.clear_live()
        self._show_cursor()

    def blank(self) -> None:
        if not self._last_was_blank:
            self.write("")

    def write(self, value: str, *, tone: Tone = "progress") -> None:
        self.clear_live()
        self._show_cursor()
        print(self._styled(value, tone=tone), file=self.stream, flush=True)
        self._last_was_blank = not value

    def wrapped(
        self,
        value: str,
        *,
        prefix: str,
        continuation: str | None = None,
        tone: Tone = "progress",
    ) -> None:
        continuation = prefix if continuation is None else continuation
        lines = wrap_display(
            one_line(value),
            width=max(self.width - display_width(prefix), 1),
        ) or [""]
        self.write(f"{prefix}{lines[0]}", tone=tone)
        for line in lines[1:]:
            self.write(f"{continuation}{line}", tone=tone)

    def apply(self, update: ProgressUpdate) -> None:
        """Append finalized rows and atomically replace the live snapshot."""

        self.clear_live()
        for block in update.finalized:
            for row in block.rows:
                self._write_progress_row(row)
        self.show_live_rows([row for block in update.live for row in block.rows])

    def show_live_rows(self, rows: list[ProgressRow]) -> None:
        if not self.tty:
            return
        self.clear_live()
        if not rows:
            self._show_cursor()
            return
        self._hide_cursor()
        live_lines: list[tuple[str, Tone]] = []
        for row in rows:
            lines = (
                self._wrapped_row_lines(row.text)
                if row.wrap_live
                else [truncate(row.text, self.width)]
            )
            live_lines.extend((line, row.tone) for line in lines)
        rendered = "\n".join(self._styled(line, tone=tone) for line, tone in live_lines)
        self.stream.write(rendered)
        self.stream.flush()
        self._live_lines = [line for line, _tone in live_lines]

    def clear_live(self) -> None:
        if not self._live_lines:
            return
        self.stream.write("\r\x1b[2K")
        for _line in self._live_lines[1:]:
            self.stream.write("\x1b[1A\r\x1b[2K")
        self.stream.flush()
        self._live_lines.clear()

    def _hide_cursor(self) -> None:
        if self.tty and not self._cursor_hidden:
            self.stream.write(_CURSOR_HIDE)
            self.stream.flush()
            self._cursor_hidden = True

    def _show_cursor(self) -> None:
        if self.tty and self._cursor_hidden:
            self.stream.write(_CURSOR_SHOW)
            self.stream.flush()
            self._cursor_hidden = False

    def _styled(self, value: str, *, tone: Tone) -> str:
        if not self.tty or not value:
            return value
        style = _ANSI_STYLE[tone]
        return f"{style}{value}{_ANSI_RESET}" if style else value

    def _write_progress_row(self, row: ProgressRow) -> None:
        if display_width(row.text) <= self.width:
            self.write(row.text, tone=row.tone)
            return
        prefix, content = split_hanging_prefix(row.text)
        if display_width(prefix) >= self.width:
            self.wrapped(row.text, prefix="", continuation="", tone=row.tone)
            return
        self.wrapped(
            content,
            prefix=prefix,
            continuation=" " * len(prefix),
            tone=row.tone,
        )

    def _wrapped_row_lines(self, value: str) -> list[str]:
        prefix, content = split_hanging_prefix(value)
        prefix_width = display_width(prefix)
        if prefix_width >= self.width:
            return wrap_display(value, self.width)
        lines = wrap_display(
            content,
            width=max(self.width - prefix_width, 1),
        )
        continuation = " " * len(prefix)
        return [
            f"{prefix if index == 0 else continuation}{line}"
            for index, line in enumerate(lines)
        ]
