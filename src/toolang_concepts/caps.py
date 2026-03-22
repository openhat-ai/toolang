"""Capability concepts shared by Toolang and toolang_caps."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

CAP_KINDS = ("skill", "service", "prompt", "psyche")
TEXT_CAP_KINDS = ("service", "prompt", "psyche")
CapKind = Literal["skill", "service", "prompt", "psyche"]
InlineCapKind = Literal["service", "prompt", "psyche"]

SECTION_BY_KIND = {
    "skill": "skills",
    "service": "services",
    "prompt": "prompts",
    "psyche": "psyches",
}


def section_name(kind: CapKind) -> str:
    """Return the plural directory section name for one cap kind."""

    return SECTION_BY_KIND[kind]


def refs_attr_name(kind: CapKind) -> str:
    """Return the sync-state attribute name for one cap kind."""

    return section_name(kind)


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


class CapContent(BaseModel):
    """Parsed text capability content defined in authored source."""

    kind: InlineCapKind
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
    front_matter: dict[str, Any] | None = None
    content: str = ""
    raw_text: str = ""
    entry_path: str | None = None
    asset_files: list[str] = Field(default_factory=list)
    ref: str | None = None
    repo: str | None = None
    source_path: str | None = None
    rev: str | None = None


class CapRef(BaseModel):
    """Resolved remote reference for one capability import."""

    kind: CapKind
    name: str
    ref: str
    repo: str
    path: str
    rev: str
