"""Indentation of authored text within grammar-owned block boundaries."""

from collections.abc import Sequence


def text_indent_width(line: str) -> int:
    """Measure a source prefix using the grammar's eight-column tab stops."""

    prefix = line[: len(line) - len(line.lstrip(" \t"))]
    return len(prefix.expandtabs(8))


def dedent_text_lines(lines: Sequence[str]) -> list[str]:
    """Remove the shared margin without changing relative text indentation."""

    margin = min((text_indent_width(line) for line in lines if line.strip()), default=0)
    return [
        " " * (text_indent_width(line) - margin) + line.lstrip(" \t").rstrip()
        if line.strip()
        else ""
        for line in lines
    ]
