"""Minimal JSON webhook channel plugin."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, cast, get_args

from toolang.concepts.channel import InboundDelivery, OutboundMessage, ReplyTarget
from toolang.concepts.execution import MessageOrigin, MessageSender

from ..contracts import ChannelState, DeliveryResult, HookRequest, PluginHealth, PollResult

_ORIGINS = frozenset(get_args(MessageOrigin))
_SENDERS = frozenset(get_args(MessageSender))


@dataclass(slots=True)
class WebhookPlugin:
    """Minimal JSON webhook plugin used for hook-based ingress."""

    config: dict[str, Any]

    def poll(self, state: ChannelState) -> PollResult:
        """Webhook plugins do not support polling."""

        return PollResult(next_state=state)

    def decode_hook(self, request: HookRequest) -> InboundDelivery | None:
        """Decode one JSON hook request into an inbound delivery."""

        try:
            payload = json.loads(request.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None

        origin = _normalized_origin(self.config.get("origin", payload.get("origin", "invoke")))
        sender = _normalized_sender(self.config.get("sender", payload.get("sender", "service")))
        thread_field = str(self.config.get("thread_field", "thread_id"))
        text_field = str(self.config.get("text_field", "text"))
        thread_id = _optional_text(payload.get(thread_field))
        text = _optional_text(payload.get(text_field))
        if thread_id is None or text is None:
            return None

        channel = _optional_text(self.config.get("channel", payload.get("channel")))
        reply_target = _reply_target_from_payload(payload.get("reply_target"))
        meta = payload.get("meta")
        return InboundDelivery(
            origin=origin,
            channel=channel,
            sender=sender,
            thread_id=thread_id,
            text=text,
            reply_target=reply_target,
            meta=dict(meta) if isinstance(meta, dict) else {},
        )

    def deliver(self, target: ReplyTarget, message: OutboundMessage) -> DeliveryResult:
        """Webhook ingress plugins do not support outbound delivery."""

        return DeliveryResult(
            ok=False,
            detail="webhook plugin does not support outbound delivery",
            meta={"channel": target.channel, "address": target.address, "text": message.text},
        )

    def health(self) -> PluginHealth:
        """Report current plugin health."""

        return PluginHealth(ok=True)


def create_webhook_plugin(config: dict[str, Any]) -> WebhookPlugin:
    """Create one builtin webhook plugin instance."""

    return WebhookPlugin(config=dict(config))


def _normalized_origin(value: Any) -> MessageOrigin:
    text = _optional_text(value)
    if text is None or text not in _ORIGINS:
        return "invoke"
    return cast(MessageOrigin, text)


def _normalized_sender(value: Any) -> MessageSender:
    text = _optional_text(value)
    if text is None or text not in _SENDERS:
        return "service"
    return cast(MessageSender, text)


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text


def _reply_target_from_payload(value: Any) -> ReplyTarget | None:
    if not isinstance(value, dict):
        return None
    channel = _optional_text(value.get("channel"))
    address = _optional_text(value.get("address"))
    if channel is None or address is None:
        return None
    thread_id = _optional_text(value.get("thread_id"))
    meta = value.get("meta")
    return ReplyTarget(
        channel=channel,
        address=address,
        thread_id=thread_id,
        meta=dict(meta) if isinstance(meta, dict) else {},
    )
