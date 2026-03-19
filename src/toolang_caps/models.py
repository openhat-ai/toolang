from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

CAP_KINDS = ("skills", "services", "prompts", "psyches")
CapKind = Literal["skills", "services", "prompts", "psyches"]


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


class SyncedCap(BaseModel):
    kind: CapKind
    name: str
    language: str | None = None
    body: str = ""
    params: list[CapParam] = Field(default_factory=list)

