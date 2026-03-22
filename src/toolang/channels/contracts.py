"""Contracts for Toolang channel plugins."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from toolang.concepts.channel import InboundDelivery, OutboundMessage, ReplyTarget


@dataclass(frozen=True, slots=True)
class ChannelState:
    """One plugin-owned polling state snapshot."""

    cursor: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)


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
class DeliveryResult:
    """One outbound delivery attempt result."""

    ok: bool
    remote_id: str | None = None
    detail: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PluginHealth:
    """One health-check result for a channel plugin."""

    ok: bool
    detail: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)


class ChannelPlugin(Protocol):
    """Protocol implemented by one loaded channel plugin instance."""

    def poll(self, state: ChannelState) -> list[InboundDelivery]:
        """Poll for zero or more inbound deliveries."""

    def decode_hook(self, request: HookRequest) -> InboundDelivery | None:
        """Decode one inbound hook request."""

    def deliver(self, target: ReplyTarget, message: OutboundMessage) -> DeliveryResult:
        """Deliver one outbound message."""

    def health(self) -> PluginHealth:
        """Report current plugin health."""


ChannelPluginFactory = Callable[[dict[str, Any]], ChannelPlugin]
