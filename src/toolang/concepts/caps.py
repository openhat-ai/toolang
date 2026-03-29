"""Capability concepts shared across Toolang modules."""

from __future__ import annotations

from typing import Any, Literal, cast

import frontmatter
from pydantic import BaseModel, ConfigDict, Field, model_validator

CapKind = Literal["skill", "service", "prompt", "psyche"]


class CapEntry(BaseModel):
    """An authored capability entry pointing to a ref or a local path."""

    ref: str | None = None
    path: str | None = None

    @model_validator(mode="after")
    def validate_location(self) -> "CapEntry":
        """Require exactly one authored location."""

        if bool(self.ref) == bool(self.path):
            raise ValueError("Cap entries must define exactly one of 'ref' or 'path'.")
        return self


class CapParam(BaseModel):
    """One declared parameter on a text capability."""

    name: str
    optional: bool = False


class _FrontmatterBase(BaseModel):
    """Base model for capability front matter with room for extension."""

    model_config = ConfigDict(extra="allow")


class ServiceFrontmatter(_FrontmatterBase):
    """Structured front matter for a service capability."""

    transport: str | None = None
    target: str | None = None
    description: str | None = None
    command: str | list[str] | None = None
    args: list[str] = Field(default_factory=list)
    port: int | None = None
    env: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_service_envs(self) -> "ServiceFrontmatter":
        """Keep service env declarations stable and predictable."""

        normalized = [_normalize_service_env_var(item) for item in self.env]
        deduped: list[str] = []
        for item in normalized:
            if item not in deduped:
                deduped.append(item)
        self.env = deduped
        return self

    def required_env_vars(self) -> list[str]:
        """Return concrete env-var names required by this service."""

        return list(self.env)


class PromptFrontmatter(_FrontmatterBase):
    """Structured front matter for a prompt capability."""

    description: str | None = None


class PsycheFrontmatter(_FrontmatterBase):
    """Structured front matter for a psyche capability."""

    description: str | None = None


class SkillFrontmatter(_FrontmatterBase):
    """Structured front matter for a skill capability."""

    description: str | None = None


CapFrontmatter = (
    ServiceFrontmatter | PromptFrontmatter | PsycheFrontmatter | SkillFrontmatter
)
_FRONTMATTER_MODEL_BY_KIND = {
    "service": ServiceFrontmatter,
    "prompt": PromptFrontmatter,
    "psyche": PsycheFrontmatter,
    "skill": SkillFrontmatter,
}


def _normalize_service_env_var(value: str) -> str:
    text = str(value).strip()
    if not text:
        raise ValueError("service env vars may not be empty.")
    return text


def parse_front_matter(
    kind: CapKind,
    value: dict[str, Any] | CapFrontmatter | None,
) -> CapFrontmatter | None:
    """Normalize one capability front matter value to its typed model."""

    if value is None:
        return None
    if isinstance(
        value,
        (ServiceFrontmatter, PromptFrontmatter, PsycheFrontmatter, SkillFrontmatter),
    ):
        return value
    return _FRONTMATTER_MODEL_BY_KIND[kind].model_validate(value)


class CapContent(BaseModel):
    """Parsed capability content defined in authored source."""

    kind: CapKind
    name: str
    language: str | None = None
    raw_text: str = ""
    params: list[CapParam] = Field(default_factory=list)


class CapMarkdownSchema(BaseModel):
    """Structured Markdown schema derived from one capability document."""

    front_matter: CapFrontmatter | None = None
    body: str = ""

    @classmethod
    def for_kind(
        cls,
        kind: CapKind,
        *,
        body: str = "",
        front_matter: dict[str, Any] | CapFrontmatter | None = None,
    ) -> "CapMarkdownSchema":
        """Build one typed Markdown schema for the selected capability kind."""

        return cls(
            front_matter=parse_front_matter(kind, front_matter),
            body=body,
        )


class CapDocument(BaseModel):
    """Raw capability content plus any parsed structured Markdown schema."""

    kind: CapKind
    language: str | None = None
    raw_text: str = ""
    front_matter: CapFrontmatter | None = None
    body: str = ""

    @classmethod
    def parse(
        cls,
        kind: CapKind,
        language: str | None,
        raw_text: str,
    ) -> "CapDocument":
        """Parse one authored capability document without changing raw content."""

        if language != "md":
            return cls(
                kind=kind,
                language=language,
                raw_text=raw_text,
                body=raw_text,
            )

        post = frontmatter.loads(raw_text)
        metadata = dict(post.metadata) or None
        return cls(
            kind=kind,
            language=language,
            raw_text=raw_text,
            front_matter=parse_front_matter(kind, metadata),
            body=post.content,
        )

    @classmethod
    def compose_markdown(
        cls,
        kind: CapKind,
        *,
        body: str,
        front_matter: dict[str, Any] | CapFrontmatter | None = None,
    ) -> "CapDocument":
        """Compose one Markdown capability document from structured schema."""

        typed_front_matter = parse_front_matter(kind, front_matter)
        metadata = (
            {}
            if typed_front_matter is None
            else typed_front_matter.model_dump(mode="python", exclude_none=True)
        )
        return cls(
            kind=kind,
            language="md",
            raw_text=_render_markdown_document(body, metadata),
            front_matter=typed_front_matter,
            body=body,
        )

    def markdown_schema(self) -> CapMarkdownSchema:
        """Return the structured Markdown schema for this document."""

        return CapMarkdownSchema(
            front_matter=self.front_matter,
            body=self.body,
        )


class CapSidecar(BaseModel):
    """Materialized sidecar metadata stored for one synced capability."""

    kind: CapKind
    name: str
    path: str
    language: str | None = None
    params: list[CapParam] = Field(default_factory=list)
    front_matter: CapFrontmatter | None = None
    content: str = ""
    raw_text: str = ""
    entry_path: str | None = None
    asset_files: list[str] = Field(default_factory=list)
    ref: str | None = None
    repo: str | None = None
    source_path: str | None = None
    rev: str | None = None

    @model_validator(mode="before")
    @classmethod
    def normalize_front_matter(cls, data: object) -> object:
        """Type front matter using the sidecar kind before model validation."""

        if not isinstance(data, dict):
            return data
        payload = cast(dict[str, Any], data)
        kind = payload.get("kind")
        if kind not in _FRONTMATTER_MODEL_BY_KIND:
            return data
        return {
            **payload,
            "front_matter": parse_front_matter(kind, payload.get("front_matter")),
        }


class CapRef(BaseModel):
    """Resolved remote reference for one capability import."""

    kind: CapKind
    name: str
    ref: str
    repo: str
    path: str
    rev: str


def parse_cap_body(kind: CapKind, language: str | None, raw_text: str) -> CapDocument:
    """Parse one capability document, extracting typed Markdown schema."""

    return CapDocument.parse(kind, language, raw_text)


ParsedCapBody = CapDocument


def _render_markdown_document(content: str, front_matter: dict[str, Any]) -> str:
    body = content.rstrip("\n")
    if not front_matter:
        return body + "\n"
    post = frontmatter.Post(body, **front_matter)
    return frontmatter.dumps(post).rstrip() + "\n"
