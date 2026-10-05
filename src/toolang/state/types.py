"""Shared capability-state vocabulary and scalar types."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from .errors import StateDiagnostic


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


@dataclass(frozen=True, slots=True)
class StateFileDifference:
    scope: Literal["root", "home"]
    key: str
    disk_digest: str | None
    state_digest: str | None


StateSyncError = Literal["state_rejected", "io_error"]


@dataclass(frozen=True, slots=True)
class StateSyncResult:
    """One publication receipt or a rejected source with its last-valid State."""

    revision: str | None
    files: tuple[StateFile, ...]
    error: StateSyncError | None = None
    message: str | None = None
    differences: tuple[StateFileDifference, ...] | None = None
    diagnostics: tuple[StateDiagnostic, ...] = ()

    def to_data(self) -> dict[str, object]:
        data: dict[str, object] = {
            "revision": self.revision,
            "files": [item.to_data() for item in self.files],
        }
        if self.error is not None:
            data.update(
                error=self.error,
                message=self.message,
                differences=(
                    [asdict(item) for item in self.differences]
                    if self.differences is not None
                    else None
                ),
                diagnostics=[asdict(item) for item in self.diagnostics],
            )
        return data


StateSync = Callable[[], Awaitable[StateSyncResult]]
