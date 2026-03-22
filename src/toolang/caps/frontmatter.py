"""Markdown front matter parsing for capability bodies."""

from __future__ import annotations

import frontmatter
from pydantic import BaseModel
from toolang.concepts.caps import (
    CapFrontmatter,
    CapKind,
    parse_front_matter,
)


class ParsedCapBody(BaseModel):
    """Parsed markdown body with optional front matter metadata."""

    front_matter: CapFrontmatter | None = None
    content: str = ""


def parse_cap_body(kind: CapKind, language: str | None, raw_text: str) -> ParsedCapBody:
    """Parse capability text, extracting front matter for Markdown bodies."""

    if language != "md":
        return ParsedCapBody(content=raw_text)

    post = frontmatter.loads(raw_text)
    metadata = dict(post.metadata) or None
    return ParsedCapBody(
        front_matter=parse_front_matter(kind, metadata),
        content=post.content,
    )
