"""Shared work scheduling vocabulary and scalar types."""

from typing import Literal


JobStatus = Literal["todo", "running", "done", "failed", "canceled"]
JobTrigger = Literal["scheduler", "manual", "reopen"]
