from __future__ import annotations

from typing import Any

from toolang.errors import ToolangError
from toolang_concepts.execution import Message, MessageSender


def chat_message(
    *,
    channel: str,
    sender: MessageSender,
    thread_id: str,
    text: str,
    meta: dict[str, Any] | None = None,
) -> Message:
    if not channel.strip():
        raise ToolangError("Chat messages require a non-empty channel.")
    return Message(
        origin="chat",
        channel=channel,
        sender=sender,
        thread_id=thread_id,
        text=text,
        meta=dict(meta or {}),
    )


def context_prompt(message: Message) -> str:
    channel = message.channel if message.channel is not None else "null"
    return (
        "Message context:\n"
        f"- origin: {message.origin}\n"
        f"- channel: {channel}\n"
        f"- sender: {message.sender}\n"
        f"- thread_id: {message.thread_id}"
    )
