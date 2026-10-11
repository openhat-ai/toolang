"""Bundled templates for authored catalog entries."""

from __future__ import annotations

from dataclasses import dataclass
from importlib.resources import files
from typing import Literal

import frontmatter

TemplateKind = Literal[
    "agent", "script", "skill", "prompt", "service", "psyche", "chore"
]


@dataclass(frozen=True, slots=True)
class TemplateSpec:
    """One bundled template file."""

    kind: TemplateKind
    path: str
    description: str | None
    raw_text: str


def load_template(kind: TemplateKind) -> TemplateSpec:
    """Load the bundled default template for one kind."""

    extension = "too" if kind in {"agent", "script"} else "md"
    path = f"{kind}.default.{extension}"
    raw_text = (
        files("toolang.catalog.templates").joinpath(path).read_text(encoding="utf-8")
    )
    return TemplateSpec(
        kind=kind,
        path=path,
        description=_template_description(kind, raw_text),
        raw_text=raw_text,
    )


def _template_description(kind: TemplateKind, raw_text: str) -> str | None:
    if not raw_text.lstrip().startswith("---"):
        return None
    post = frontmatter.loads(raw_text)
    value = post.metadata.get("title" if kind == "chore" else "description")
    text = str(value).strip() if value is not None else ""
    return text or None
