"""Shared authored-catalog vocabulary and scalar types."""

from typing import Literal


JobKind = Literal["task", "chore"]
JobStage = Literal["draft", "ready", "archived"]

JOB_KINDS: tuple[JobKind, ...] = ("task", "chore")
JOB_STAGES: tuple[JobStage, ...] = ("draft", "ready", "archived")
DEFAULT_CHORE_SCHEDULE = "FREQ=HOURLY;INTERVAL=1"
