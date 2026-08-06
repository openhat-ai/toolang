"""Shared runtime scheduling vocabulary."""

from dataclasses import dataclass
from typing import Literal


JobStatus = Literal["pending", "running", "done", "failed", "canceled"]
JobTrigger = Literal["source", "schedule", "manual"]
FileRequestStatus = Literal["running", "finished", "failed", "canceled"]


@dataclass(frozen=True, slots=True)
class FileSnapshot:
    """One stable regular file observed by the legacy inbox runner."""

    watch_root: str
    relative_path: str
    absolute_path: str
    size: int
    mtime_ns: int
    fingerprint: str
