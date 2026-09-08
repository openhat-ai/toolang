"""Shared runtime scheduling vocabulary."""

from typing import Literal


JobStatus = Literal["pending", "running", "done", "failed", "canceled"]
JobTrigger = Literal["source", "schedule", "manual"]
