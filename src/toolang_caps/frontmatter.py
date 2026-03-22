"""Markdown front matter parsing for inline text caps."""

from __future__ import annotations

from typing import Any

import frontmatter
from pydantic import BaseModel


class ParsedCapBody(BaseModel):
    """Parsed markdown body with optional front matter metadata."""

    front_matter: dict[str, Any] | None = None
    content: str = ""


def parse_cap_body(language: str | None, raw_text: str) -> ParsedCapBody:
    """Parse inline cap text, extracting front matter for Markdown bodies."""

    if language != "md":
        return ParsedCapBody(content=raw_text)

    post = frontmatter.loads(raw_text)
    metadata = dict(post.metadata) or None
    return ParsedCapBody(front_matter=metadata, content=post.content)
