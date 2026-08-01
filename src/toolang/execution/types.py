"""Shared execution vocabulary and scalar types."""

from enum import StrEnum
from typing import Literal


StepPath = str
RunId = str

RunStatus = Literal["pending", "running", "finished", "failed", "canceled"]
StepStatus = Literal["running", "finished", "failed", "canceled"]
ControlStatus = Literal["pending", "finished", "canceled", "failed"]

StepKind = Literal[
    "run",
    "agent",
    "human",
    "model",
    "tool",
    "par",
    "loop",
    "system",
]
ControlTiming = Literal["immediate", "next_step", "next_call"]
RunControlKind = Literal["start", "steer", "stop"]
ThreadControlKind = Literal["create", "fork", "rewind"]
ThreadPeerType = Literal["user", "agent"]


class ThreadPrefix(StrEnum):
    """Canonical prefixes for locally issued thread ids."""

    SCRIPT = "script"
    WEB = "web"
    TERM = "term"
