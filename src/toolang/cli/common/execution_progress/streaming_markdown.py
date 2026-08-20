"""Incremental Markdown source partitioning for progress scrollback."""

from __future__ import annotations

from rich.markdown import Markdown


def split_stable_markdown(source: str) -> tuple[str, str]:
    """Split stable leading blocks from the final mutable Markdown block.

    Each returned fragment is rendered independently, so later source cannot
    reopen or restyle a committed fragment.
    """

    if not source:
        return "", ""
    starts = sorted(
        {
            token.map[0]
            for token in Markdown(source).parsed
            if token.level == 0 and token.map is not None
        }
    )
    if len(starts) < 2:
        return "", source
    lines = source.splitlines(keepends=True)
    cutoff = sum(len(line) for line in lines[: starts[-1]])
    return source[:cutoff], source[cutoff:]
