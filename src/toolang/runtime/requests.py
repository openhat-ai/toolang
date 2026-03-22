"""Ingress request shapes for the runtime host and scheduler."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .messages import Message

TurnRequestKind = Literal["invoke", "chat"]


@dataclass(frozen=True, slots=True)
class TurnRequest:
    """One normalized turn submission entering the runtime scheduler."""

    kind: TurnRequestKind
    thread_id: str | None
    message: Message | None = None
