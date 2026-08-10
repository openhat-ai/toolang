"""Shared channel protocols."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..types.channel import (
    ChannelContext,
    ChannelState,
    DeliveryResult,
    HookRequest,
    InboundDelivery,
    OutboundMessage,
    PluginHealth,
    PollResult,
    ReplyTarget,
)


@runtime_checkable
class AgentChannel(Protocol):
    """Protocol implemented by one loaded channel plugin instance."""

    def poll(self, state: ChannelState, context: ChannelContext) -> PollResult:
        """Poll for zero or more inbound deliveries."""

    def decode_hook(
        self, request: HookRequest, context: ChannelContext
    ) -> InboundDelivery | None:
        """Decode one inbound hook request."""

    def deliver(
        self,
        target: ReplyTarget,
        message: OutboundMessage,
        context: ChannelContext,
    ) -> DeliveryResult:
        """Deliver one outbound message."""

    def health(self, context: ChannelContext) -> PluginHealth:
        """Report current plugin health."""
