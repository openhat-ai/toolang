"""Rich terminal output owned by the Script progress renderer."""

from __future__ import annotations

import shutil
from typing import Literal, TextIO

from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.text import Text

from ..execution_progress import ProgressBlock, ProgressRow, ProgressUpdate
from ..execution_progress.config import DEFAULT_MAX_PROGRESS_WIDTH
from ..execution_progress.formatting import one_line
from ..execution_progress.rich_rendering import (
    TERMINAL_MARKDOWN_THEME,
    progress_block_renderable,
)

Tone = Literal["progress", "normal", "active", "error", "warning"]

_STYLES: dict[Tone, str] = {
    "progress": "dim",
    "normal": "none",
    "active": "none",
    "error": "red",
    "warning": "yellow",
}


class ProgressConsole:
    """Append committed fragments and render one replaceable Rich Live area."""

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
        self.console = Console(
            file=stream,
            width=self.width,
            color_system="standard" if self.tty else None,
            force_terminal=self.tty,
            highlight=False,
            legacy_windows=False,
            theme=TERMINAL_MARKDOWN_THEME,
            _environ={"COLUMNS": str(self.width), "LINES": "24"},
        )
        self._live: Live | None = None
        self._live_rows: list[ProgressRow] = []

    def close(self) -> None:
        self.clear_live()
        if self._live is not None:
            self._live.stop()
            self._live = None

    def write(self, value: str, *, tone: Tone = "progress") -> None:
        self.clear_live()
        self.console.print(Text(value, style=_STYLES[tone], no_wrap=True))

    def wrapped(
        self,
        value: str,
        *,
        prefix: str,
        continuation: str | None = None,
        tone: Tone = "progress",
    ) -> None:
        del continuation
        self.clear_live()
        block = ProgressBlock(
            "script:write",
            (ProgressRow(f"{prefix}{one_line(value)}", tone),),
        )
        self.console.print(
            progress_block_renderable(block, live=False, max_width=self.width)
        )

    def write_renderable(self, value: RenderableType) -> None:
        """Append one complete shared Rich renderable."""

        self.clear_live()
        self.console.print(value)

    def apply(self, update: ProgressUpdate) -> None:
        """Append committed fragments and atomically replace the live snapshot."""

        committed = [
            progress_block_renderable(block, live=False, max_width=self.width)
            for block in update.committed
        ]
        live_blocks = [
            progress_block_renderable(block, live=True, max_width=self.width)
            for block in update.live
        ]
        self._live_rows = [row for block in update.live for row in block.rows]
        self._set_live(live_blocks)
        for renderable in committed:
            self.console.print(renderable)

    def show_live_rows(self, rows: list[ProgressRow]) -> None:
        block = ProgressBlock("script:live", tuple(rows))
        self._set_live(
            [progress_block_renderable(block, live=True, max_width=self.width)]
            if rows
            else []
        )
        self._live_rows = list(rows)

    def clear_live(self) -> None:
        self._live_rows.clear()
        if self._live is not None:
            self._live.update(Group(), refresh=True)

    def _set_live(self, renderables: list[RenderableType]) -> None:
        if not self.tty:
            return
        renderable = Group(*renderables)
        if self._live is None:
            if not renderables:
                return
            self._live = Live(
                renderable,
                console=self.console,
                auto_refresh=False,
                transient=True,
                redirect_stdout=False,
                redirect_stderr=False,
                vertical_overflow="crop",
            )
            self._live.start(refresh=True)
            return
        self._live.update(renderable, refresh=True)
