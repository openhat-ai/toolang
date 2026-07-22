"""Shared work vocabulary and value types."""

from dataclasses import dataclass
from typing import Literal


JobStatus = Literal["todo", "running", "done", "failed", "canceled"]
JobTrigger = Literal["scheduler", "manual", "reopen"]
FileRequestStatus = Literal["running", "finished", "failed", "canceled"]


@dataclass(frozen=True, slots=True)
class FileSnapshot:
    """One stable regular file observed under an inbox."""

    watch_root: str
    relative_path: str
    absolute_path: str
    size: int
    mtime_ns: int
    fingerprint: str
