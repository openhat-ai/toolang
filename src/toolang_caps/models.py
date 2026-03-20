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
    return SECTION_BY_KIND[kind]


def refs_attr_name(kind: CapKind) -> str:
    return section_name(kind)


class CapEntry(BaseModel):
    ref: str | None = None
    path: str | None = None

    @model_validator(mode="after")
    def validate_location(self) -> "CapEntry":
        if bool(self.ref) == bool(self.path):
            raise ValueError("Cap entries must define exactly one of 'ref' or 'path'.")
        return self


class CapParam(BaseModel):
    name: str
    optional: bool = False


class InlineCap(BaseModel):
    kind: InlineCapKind
    name: str
    language: str | None = None
    raw_text: str = ""
    params: list[CapParam] = Field(default_factory=list)


class InlineCapMeta(BaseModel):
    kind: InlineCapKind
    name: str
    language: str | None = None
    path: str
    params: list[CapParam] = Field(default_factory=list)
    front_matter: dict[str, Any] | None = None
    content: str = ""
    raw_text: str = ""
    ref: str | None = None
    repo: str | None = None
    source_path: str | None = None
    rev: str | None = None


class ResolvedCapRef(BaseModel):
    kind: CapKind
    name: str
    ref: str
    repo: str
    path: str
    rev: str


class SkillMeta(BaseModel):
    kind: Literal["skill"] = "skill"
    name: str
    path: str
    entry_path: str
    files: list[str] = Field(default_factory=list)
    ref: str | None = None
    repo: str | None = None
    source_path: str
    rev: str | None = None
