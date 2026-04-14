"""Shared channel helper functions."""

from __future__ import annotations

from ..types.channel import InboundDelivery, ReplyTarget


def bind_delivery(binding_name: str, delivery: InboundDelivery) -> InboundDelivery:
    """Bind one delivery to one configured channel binding name."""

    reply_target = delivery.reply_target
    if reply_target is not None and reply_target.channel != binding_name:
        reply_target = ReplyTarget(
            channel=binding_name,
            address=reply_target.address,
            thread_id=reply_target.thread_id,
            meta=dict(reply_target.meta),
        )
    if delivery.channel == binding_name and reply_target is delivery.reply_target:
        return delivery
    return InboundDelivery(
        origin=delivery.origin,
        channel=binding_name,
        sender=delivery.sender,
        thread_id=delivery.thread_id,
        text=delivery.text,
        reply_target=reply_target,
        meta=dict(delivery.meta),
    )
