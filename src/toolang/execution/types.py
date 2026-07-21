"""Shared execution vocabulary and scalar types."""

from typing import Literal


StepPath = str
RunId = str
RunLoop = str

RunStatus = Literal["pending", "running", "finished", "failed", "canceled"]
StepStatus = Literal["running", "finished", "failed", "canceled"]
CommandStatus = Literal["pending", "finished", "canceled"]

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
CommandKind = Literal["start", "steer", "stop"]
CommandApply = Literal["now", "next_step", "next_call"]

UpdateKind = Literal[
    "created",
    "started",
    "stopped",
    "removed",
    "program_changed",
    "config_changed",
    "psyche_changed",
    "prompt_changed",
    "service_changed",
    "skill_changed",
    "task_changed",
    "chore_changed",
]
EventDomain = Literal["thread", "run"]
