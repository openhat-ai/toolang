"""Shared channel value types."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast


@dataclass(frozen=True, slots=True)
class ReplyTarget:
    """One outbound reply destination."""

    channel: str
    address: str
    thread_id: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    def to_data(self) -> dict[str, object]:
        return {
            "channel": self.channel,
            "address": self.address,
            "thread_id": self.thread_id,
            "meta": dict(self.meta),
        }

    @classmethod
    def from_data(cls, data: dict[str, object]) -> "ReplyTarget":
        return cls(
            channel=str(data["channel"]),
            address=str(data["address"]),
            thread_id=str(data["thread_id"])
            if data.get("thread_id") is not None
            else None,
            meta=dict(cast(dict[str, Any], data.get("meta", {}))),
        )


@dataclass(frozen=True, slots=True)
class InboundDelivery:
    """One normalized inbound channel delivery."""

    origin: str
    channel: str | None
    sender: str
    thread_id: str
    text: str
    reply_target: ReplyTarget | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    def to_data(self) -> dict[str, object]:
        return {
            "origin": self.origin,
            "channel": self.channel,
            "sender": self.sender,
            "thread_id": self.thread_id,
            "text": self.text,
            "reply_target": self.reply_target.to_data()
            if self.reply_target is not None
            else None,
            "meta": dict(self.meta),
        }


@dataclass(frozen=True, slots=True)
class OutboundMessage:
    """One outbound message to send through a channel plugin."""

    text: str
    meta: dict[str, Any] = field(default_factory=dict)

    def to_data(self) -> dict[str, object]:
        return {
            "text": self.text,
            "meta": dict(self.meta),
        }


@dataclass(frozen=True, slots=True)
class ChannelState:
    """One plugin-owned polling state snapshot."""

    cursor: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    def to_data(self) -> dict[str, object]:
        return {
            "cursor": self.cursor,
            "meta": dict(self.meta),
        }

    @classmethod
    def from_data(cls, data: dict[str, object]) -> "ChannelState":
        return cls(
            cursor=str(data["cursor"]) if data.get("cursor") is not None else None,
            meta=dict(cast(dict[str, Any], data.get("meta", {}))),
        )


@dataclass(frozen=True, slots=True)
class PollResult:
    """One polling result with deliveries and the next plugin state."""

    deliveries: list[InboundDelivery] = field(default_factory=list)
    next_state: ChannelState = field(default_factory=ChannelState)


@dataclass(frozen=True, slots=True)
class HookRequest:
    """One normalized hook request passed into a channel plugin."""

    method: str
    path: str
    headers: dict[str, str]
    query: dict[str, str]
    body: bytes
    content_type: str | None = None


@dataclass(frozen=True, slots=True)
class ChannelContext:
    """Resolved context passed into one channel plugin call."""

    home: Path
    room: Path


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    """One outbound delivery attempt result."""

    ok: bool
    remote_id: str | None = None
    detail: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    def to_data(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "remote_id": self.remote_id,
            "detail": self.detail,
            "meta": dict(self.meta),
        }


@dataclass(frozen=True, slots=True)
class PluginHealth:
    """One health-check result for a channel plugin."""

    ok: bool
    detail: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    def to_data(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "detail": self.detail,
            "meta": dict(self.meta),
        }
