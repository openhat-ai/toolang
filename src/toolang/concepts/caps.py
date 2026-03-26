"""Capability concepts shared across Toolang modules."""

from __future__ import annotations

import re
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
    auth_env: str | None = None

    @model_validator(mode="after")
    def validate_service_envs(self) -> "ServiceFrontmatter":
        """Keep service env declarations stable and predictable."""

        normalized = [_normalize_service_env_name(item) for item in self.env]
        deduped: list[str] = []
        for item in normalized:
            if item not in deduped:
                deduped.append(item)
        self.env = deduped
        if self.auth_env is not None:
            self.auth_env = _normalize_service_env_name(self.auth_env)
            if self.auth_env not in self.env:
                raise ValueError("auth_env must also be declared in env.")
        return self

    def required_env_vars(self, service_name: str) -> list[str]:
        """Return concrete env-var names required by this service."""

        return [service_env_var_name(service_name, item) for item in self.env]

    def auth_env_var(self, service_name: str) -> str | None:
        """Return the concrete env-var name used for HTTP auth, if declared."""

        if self.auth_env is None:
            return None
        return service_env_var_name(service_name, self.auth_env)


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

_SERVICE_ENV_TOKEN_RE = re.compile(r"[^A-Za-z0-9]+")


def _normalize_service_env_name(value: str) -> str:
    text = _SERVICE_ENV_TOKEN_RE.sub("_", str(value).strip()).strip("_").lower()
    if not text:
        raise ValueError("service env names may not be empty.")
    return text


def _service_env_token(value: str) -> str:
    token = _SERVICE_ENV_TOKEN_RE.sub("_", str(value).strip()).strip("_").upper()
    if not token:
        raise ValueError("service env tokens may not be empty.")
    return token


def service_env_var_name(service_name: str, env_name: str) -> str:
    """Return the canonical .env variable name for one service requirement."""

    return f"TOOLANG_SERVICE_{_service_env_token(service_name)}_{_service_env_token(env_name)}"


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


class ParsedCapBody(BaseModel):
    """Parsed capability body with optional typed front matter metadata."""

    front_matter: CapFrontmatter | None = None
    content: str = ""


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


def parse_cap_body(kind: CapKind, language: str | None, raw_text: str) -> ParsedCapBody:
    """Parse one capability body, extracting typed front matter from Markdown."""

    if language != "md":
        return ParsedCapBody(content=raw_text)

    post = frontmatter.loads(raw_text)
    metadata = dict(post.metadata) or None
    return ParsedCapBody(
        front_matter=parse_front_matter(kind, metadata),
        content=post.content,
    )
