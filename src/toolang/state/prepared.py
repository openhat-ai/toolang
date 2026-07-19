"""Prepared cap value objects shared by state and runtime consumers."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

import frontmatter

from ..common.immutable import freeze_mapping, mutable_data

PreparedVisibility = Literal["shared", "private"]
EntryKind = Literal["psyche", "skill", "service", "prompt"]
EntryShape = Literal["file", "dir"]
SourceOrigin = Literal["local", "remote"]
SourceForm = Literal["inline", "ref", "wired", "file"]


@dataclass(frozen=True, slots=True)
class PreparedSource:
    """Source identity and provenance for one prepared cap."""

    origin: SourceOrigin
    form: SourceForm
    path: str
    updated_at: str
    fingerprint: str
    authored_ref: str | None = None
    line: int | None = None

    def to_data(self) -> dict[str, object]:
        data: dict[str, object] = {
            "origin": self.origin,
            "form": self.form,
            "path": self.path,
            "updated_at": self.updated_at,
            "fingerprint": self.fingerprint,
        }
        if self.line is not None:
            data["line"] = self.line
        if self.authored_ref is not None:
            data["authored_ref"] = self.authored_ref
        return data

    @classmethod
    def from_data(cls, data: dict[str, object]) -> PreparedSource:
        raw_line = data.get("line")
        return cls(
            origin=cast(SourceOrigin, str(data["origin"])),
            form=cast(SourceForm, str(data["form"])),
            path=str(data["path"]),
            updated_at=str(data["updated_at"]),
            fingerprint=str(data["fingerprint"]),
            authored_ref=(
                str(data["authored_ref"])
                if data.get("authored_ref") is not None
                else None
            ),
            line=raw_line if isinstance(raw_line, int) else None,
        )


@dataclass(frozen=True, slots=True)
class PreparedEntry:
    """One immutable cap projected into a prepared generation."""

    kind: EntryKind
    name: str
    shape: EntryShape
    ref: str
    path: str
    source: PreparedSource
    meta: Mapping[str, object]

    def __post_init__(self) -> None:
        object.__setattr__(self, "meta", freeze_mapping(self.meta))

    def to_data(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "name": self.name,
            "shape": self.shape,
            "ref": self.ref,
            "path": self.path,
            "source": self.source.to_data(),
            "meta": mutable_data(self.meta),
        }

    def read_text(self) -> str:
        """Read this cap from its immutable generation file."""

        return Path(self.path).read_text(encoding="utf-8")

    def read_content(self) -> str:
        """Read the cap body lazily from its immutable generation file."""

        return frontmatter.loads(self.read_text()).content.strip()

    def to_snapshot(self) -> dict[str, object]:
        return self.to_data()

    @classmethod
    def from_data(cls, data: dict[str, object]) -> PreparedEntry:
        return cls(
            kind=cast(EntryKind, str(data["kind"])),
            name=str(data["name"]),
            shape=cast(EntryShape, str(data["shape"])),
            ref=str(data["ref"]),
            path=str(data["path"]),
            source=PreparedSource.from_data(cast(dict[str, object], data["source"])),
            meta=dict(cast(dict[str, object], data.get("meta", {}))),
        )
