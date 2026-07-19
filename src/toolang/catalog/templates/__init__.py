"""Bundled templates for authored catalog entries."""

from __future__ import annotations

from dataclasses import dataclass
from importlib.resources import files
import re
from typing import Literal

import frontmatter

TemplateKind = Literal["agent", "skill", "prompt", "service", "psyche", "task", "chore"]
_TEMPLATE_FILE_RE = re.compile(
    r"^(?P<kind>agent|skill|prompt|service|psyche|task|chore)\.(?P<name>[A-Za-z0-9_-]+)\.(?P<ext>md|too)$"
)


@dataclass(frozen=True, slots=True)
class TemplateSpec:
    """One bundled template file."""

    kind: TemplateKind
    name: str
    path: str
    description: str | None
    raw_text: str

    @property
    def title(self) -> str:
        return self.name.replace("_", " ").replace("-", " ").title()


def list_templates(kind: TemplateKind) -> tuple[TemplateSpec, ...]:
    """Return bundled templates for one kind."""

    items: list[TemplateSpec] = []
    for resource in files("toolang.catalog.templates").iterdir():
        if not resource.is_file():
            continue
        matched = _TEMPLATE_FILE_RE.fullmatch(resource.name)
        if matched is None or matched.group("kind") != kind:
            continue
        raw_text = resource.read_text(encoding="utf-8")
        items.append(
            TemplateSpec(
                kind=kind,
                name=matched.group("name"),
                path=resource.name,
                description=_template_description(kind, raw_text),
                raw_text=raw_text,
            )
        )
    items.sort(key=lambda item: (0 if item.name == "default" else 1, item.name))
    return tuple(items)


def load_template(kind: TemplateKind, template_name: str = "default") -> TemplateSpec:
    """Load one named template."""

    for item in list_templates(kind):
        if item.name == template_name:
            return item
    raise FileNotFoundError(f"template not found: {kind}.{template_name}")


def render_template(
    kind: TemplateKind, template_name: str = "default", **bindings: str
) -> str:
    """Render one named template with simple variable substitution."""

    text = load_template(kind, template_name).raw_text
    for key, value in bindings.items():
        text = text.replace(f"{{{{{key}}}}}", value)
    return text


def _template_description(kind: TemplateKind, raw_text: str) -> str | None:
    if not raw_text.lstrip().startswith("---"):
        return None
    post = frontmatter.loads(raw_text)
    value = post.metadata.get("title" if kind in {"task", "chore"} else "description")
    text = str(value).strip() if value is not None else ""
    return text or None
