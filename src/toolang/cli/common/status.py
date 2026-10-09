"""Shared terminal status-row presentation."""

from prompt_toolkit.utils import get_cwidth

from .execution_progress.formatting import truncate


def error_status_line(message: str, *, width: int) -> list[tuple[str, str]]:
    """Keep the red error marker in the first column and a two-cell end inset."""
    width = max(1, width)
    right = "  " if width > 2 else ""
    body_width = width - len(right)
    remaining = max(0, body_width - 1)
    detail = " ".join(message.split())
    text = f" {truncate(detail, remaining - 1)}" if remaining else ""
    cells = [
        ("class:status.error.marker", "!"),
        ("class:status.error", text),
        ("class:status", " " * max(0, body_width - get_cwidth(f"!{text}"))),
    ]
    if right:
        cells.append(("class:status", right))
    return cells
