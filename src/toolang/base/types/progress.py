"""Package-neutral progress event types."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal


ProgressStatus = Literal["pending", "running", "ok", "failed", "skipped"]
ProgressKind = Literal["prepare", "setup", "runtime"]
ProgressStage = Literal[
    "resolve",
    "fetch",
    "materialize",
    "load",
    "discover",
    "create",
    "start",
    "stop",
    "destroy",
]

_STAGES_BY_KIND: dict[ProgressKind, frozenset[ProgressStage]] = {
    "prepare": frozenset({"resolve", "fetch", "materialize"}),
    "setup": frozenset({"load", "discover"}),
    "runtime": frozenset({"create", "start", "stop", "destroy"}),
}
_STATUSES = frozenset({"pending", "running", "ok", "failed", "skipped"})


@dataclass(frozen=True, slots=True)
class ProgressEvent:
    """One progress update for a stable CLI item."""

    id: str
    kind: ProgressKind
    stage: ProgressStage
    label: str
    status: ProgressStatus
    detail: str | None = None

    def __post_init__(self) -> None:
        if not self.id or self.id != self.id.strip():
            raise ValueError("progress event requires a canonical id")
        if not self.label or self.label != self.label.strip():
            raise ValueError("progress event requires a canonical label")
        stages = _STAGES_BY_KIND.get(self.kind)
        if stages is None or self.stage not in stages:
            raise ValueError(
                f"invalid progress kind-stage pair: {self.kind}.{self.stage}"
            )
        if self.status not in _STATUSES:
            raise ValueError(f"invalid progress status: {self.status}")


ProgressSink = Callable[[ProgressEvent], None]
