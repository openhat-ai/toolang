"""Incremental Markdown source partitioning for progress scrollback."""

from __future__ import annotations

from rich.markdown import Markdown


def split_stable_markdown(source: str) -> tuple[str, str, bool]:
    """Split stable leading blocks from the final mutable Markdown block.

    Each returned fragment is rendered independently, so later source cannot
    reopen or restyle a committed fragment. The boolean reports whether a
    blank separator belongs before the mutable tail.
    """

    if not source:
        return "", "", False
    starts = sorted(
        {
            token.map[0]
            for token in Markdown(source).parsed
            if token.level == 0 and token.map is not None
        }
    )
    if len(starts) < 2:
        return "", source, False
    lines = source.splitlines(keepends=True)
    tail_start = starts[-1]
    cutoff = sum(len(line) for line in lines[:tail_start])
    gap_before = tail_start > 0 and not lines[tail_start - 1].strip()
    return source[:cutoff], source[cutoff:], gap_before
