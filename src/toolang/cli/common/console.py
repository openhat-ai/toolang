"""Terminal console defaults shared by interactive commands."""

from typing import TextIO
from rich.console import Console


def terminal_console(*, width: int, file: TextIO | None = None) -> Console:
    fixed_width = max(1, width)
    return Console(
        file=file,
        width=fixed_width,
        color_system="truecolor",
        force_terminal=True,
        legacy_windows=False,
        _environ={"COLUMNS": str(fixed_width), "LINES": "24"},
    )
