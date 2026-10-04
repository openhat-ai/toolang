"""Shared capability-state vocabulary and scalar types."""

from dataclasses import dataclass
from typing import Literal


CapScope = Literal["root", "home", "here"]
EntryKind = Literal["psyche", "skill", "service", "prompt"]
EntryShape = Literal["file", "dir"]
SourceOrigin = Literal["local", "remote"]
CapForm = Literal["authored", "inline", "configured", "referenced"]
ProgramKind = Literal["agent", "flow"]
RunnableKind = Literal["agic", "flow"]


@dataclass(frozen=True, slots=True)
class StateFile:
    """Raw authored file identity captured by one immutable State."""

    scope: Literal["root", "home"]
    key: str
    digest: str

    def to_data(self) -> dict[str, str]:
        return {"scope": self.scope, "key": self.key, "digest": self.digest}
