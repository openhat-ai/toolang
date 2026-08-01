"""Package-neutral text composition helpers."""

from __future__ import annotations


def join_paragraphs(*parts: str) -> str:
    """Join non-empty stripped text fragments as paragraphs."""

    return "\n\n".join(part.strip() for part in parts if part.strip()).strip()
