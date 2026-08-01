"""Terminal output owned by the script progress renderer."""

from __future__ import annotations

import shutil
import textwrap
from typing import Literal, TextIO

from .formatting import one_line, truncate

Tone = Literal["progress", "active", "error", "warning"]

_ANSI_STYLE: dict[Tone, str] = {
    "progress": "\x1b[2m",
    "active": "",
    "error": "\x1b[31m",
    "warning": "\x1b[33m",
}
_ANSI_RESET = "\x1b[0m"


class ProgressConsole:
    """Write stable and replaceable progress blocks to one stream."""

    def __init__(self, stream: TextIO, *, width: int | None = None) -> None:
        self.stream = stream
        self.tty = bool(getattr(stream, "isatty", lambda: False)())
        detected = (
            shutil.get_terminal_size(fallback=(100, 24)).columns
            if self.tty
            else 100
        )
        self.width = max(width or detected, 40)
        self._live_lines: list[str] = []
        self._last_was_blank = False

    def close(self) -> None:
        self.clear_live()

    def blank(self) -> None:
        if not self._last_was_blank:
            self.write("")

    def write(self, value: str, *, tone: Tone = "progress") -> None:
        self.clear_live()
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
        lines = textwrap.wrap(
            one_line(value),
            width=max(self.width - len(prefix), 10),
            break_long_words=True,
            break_on_hyphens=False,
        ) or [""]
        self.write(f"{prefix}{lines[0]}", tone=tone)
        for line in lines[1:]:
            self.write(f"{continuation}{line}", tone=tone)

    def show_live(self, lines: list[str]) -> None:
        if not self.tty:
            return
        self.clear_live()
        rendered = "\n".join(
            self._styled(truncate(line, self.width), tone="active")
            for line in lines
        )
        self.stream.write(rendered)
        self.stream.flush()
        self._live_lines = list(lines)

    def clear_live(self) -> None:
        if not self._live_lines:
            return
        self.stream.write("\r\x1b[2K")
        for _line in self._live_lines[1:]:
            self.stream.write("\x1b[1A\r\x1b[2K")
        self.stream.flush()
        self._live_lines.clear()

    def _styled(self, value: str, *, tone: Tone) -> str:
        if not self.tty or not value:
            return value
        style = _ANSI_STYLE[tone]
        return f"{style}{value}{_ANSI_RESET}" if style else value
