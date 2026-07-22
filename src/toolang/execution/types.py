"""Shared execution vocabulary and scalar types."""

from typing import Literal


StepPath = str
RunId = str
RunLoop = str

RunStatus = Literal["pending", "running", "finished", "failed", "canceled"]
StepStatus = Literal["running", "finished", "failed", "canceled"]
RunControlStatus = Literal["pending", "finished", "canceled", "failed"]
ThreadControlStatus = Literal["pending", "finished", "canceled", "failed"]

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
ThreadPeerType = Literal["user", "agent"]
RunControlKind = Literal["start", "steer", "stop"]
RunControlTiming = Literal["immediate", "next_step", "next_call"]
ThreadControlKind = Literal["create", "fork", "rewind"]
