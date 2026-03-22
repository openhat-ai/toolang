"""Channel concepts shared by runtime and channel plugins."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, TypeAlias

from .execution import MessageOrigin, MessageSender

ChannelName: TypeAlias = str


@dataclass(frozen=True, slots=True)
class ReplyTarget:
    """One outbound reply destination resolved by a channel integration."""

    channel: ChannelName
    address: str
    thread_id: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class OutboundMessage:
    """One normalized outbound message payload."""

    text: str
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class InboundDelivery:
    """One normalized inbound delivery emitted by a channel plugin."""

    origin: MessageOrigin
    channel: ChannelName | None
    sender: MessageSender
    thread_id: str
    text: str
    reply_target: ReplyTarget | None = None
    meta: dict[str, Any] = field(default_factory=dict)
