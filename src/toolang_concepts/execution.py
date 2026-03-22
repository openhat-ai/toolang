"""Runtime execution concepts."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

MessageOrigin = Literal["invoke", "chat", "task", "chore", "will"]
MessageSender = Literal["owner", "peer", "guest", "self"]
RuntimeLoop = Literal["server", "poll", "hook", "pulse"]
ExecutionStrategy = Literal["direct", "react"]


@dataclass(frozen=True, slots=True)
class Message:
    """One normalized runtime input message."""

    origin: MessageOrigin
    channel: str | None
    sender: MessageSender
    thread_id: str
    text: str
    meta: dict[str, Any] = field(default_factory=dict)
