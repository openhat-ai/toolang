"""Shared runtime scheduling vocabulary."""

from typing import Literal


ChoreStatus = Literal["pending", "running", "done", "failed", "canceled"]
ChoreTrigger = Literal["source", "schedule", "manual"]
