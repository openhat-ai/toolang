"""Capability concepts shared across Toolang modules."""

from __future__ import annotations

from typing import Any, Literal, cast

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
