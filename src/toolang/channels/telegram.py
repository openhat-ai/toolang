"""Telegram channel plugin."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx

from toolang.base.error import ToolangError
from toolang.base.protocols.channel import ChannelPlugin
from toolang.base.types.channel import (
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

DEFAULT_TELEGRAM_API_BASE = "https://api.telegram.org"
DEFAULT_POLL_TIMEOUT_SEC = 2
DEFAULT_ALLOWED_UPDATES = ("message",)


@dataclass(slots=True)
class TelegramChannel:
    """Telegram polling and outbound reply plugin."""

    config: dict[str, Any]
    _token: str = field(init=False, repr=False)
    _api_base: str = field(init=False, repr=False)
    _poll_timeout_sec: int = field(init=False, repr=False)
    _allowed_updates: list[str] = field(init=False, repr=False)
    _owner_chat_id: str | None = field(init=False, repr=False)
    _peer_chat_ids: set[str] = field(init=False, repr=False)
    _allowed_chat_ids: set[str] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._token = _required_text(self.config.get("token"), name="token")
        self._api_base = _optional_text(self.config.get("api_base")) or DEFAULT_TELEGRAM_API_BASE
        self._poll_timeout_sec = _optional_int(self.config.get("poll_timeout_sec")) or DEFAULT_POLL_TIMEOUT_SEC
        self._allowed_updates = (
            _normalized_text_list(self.config.get("allowed_updates")) or list(DEFAULT_ALLOWED_UPDATES)
        )
        self._owner_chat_id = _optional_text(self.config.get("owner_chat_id"))
        self._peer_chat_ids = set(_normalized_text_list(self.config.get("peer_chat_ids")))
        self._allowed_chat_ids = set(_normalized_text_list(self.config.get("allowed_chat_ids")))

    def poll(self, state: ChannelState, context: ChannelContext) -> PollResult:
        """Poll Telegram for inbound chat deliveries."""

        del context
        payload: dict[str, Any] = {
            "timeout": self._poll_timeout_sec,
            "allowed_updates": self._allowed_updates,
        }
        if state.cursor is not None:
            payload["offset"] = int(state.cursor)
        response = _post_telegram(
            self._api_base,
            self._token,
            "getUpdates",
            payload,
            timeout=self._poll_timeout_sec + 5.0,
        )
        updates = response.get("result")
        if not isinstance(updates, list):
            raise ToolangError("telegram getUpdates returned an invalid result payload")

        deliveries: list[InboundDelivery] = []
        max_update_id: int | None = None
        for update in updates:
            if not isinstance(update, dict):
                continue
            update_id = update.get("update_id")
            if isinstance(update_id, int):
                max_update_id = update_id if max_update_id is None else max(max_update_id, update_id)
            message = update.get("message")
            if not isinstance(message, dict):
                continue
            delivery = self._delivery_from_message(message, update_id=update_id)
            if delivery is not None:
                deliveries.append(delivery)

        next_cursor = str(max_update_id + 1) if max_update_id is not None else state.cursor
        return PollResult(
            deliveries=deliveries,
            next_state=ChannelState(cursor=next_cursor, meta=dict(state.meta)),
        )

    def decode_hook(self, request: HookRequest, context: ChannelContext) -> InboundDelivery | None:
        """Telegram polling plugin does not support generic hook decoding."""

        del request, context
        return None

    def deliver(
        self,
        target: ReplyTarget,
        message: OutboundMessage,
        context: ChannelContext,
    ) -> DeliveryResult:
        """Send one outbound Telegram message."""

        del context
        chat_id = _reply_chat_id(target)
        message_thread_id = target.meta.get("message_thread_id")
        action = _optional_text(message.meta.get("action"))
        if action is not None:
            action_payload: dict[str, Any] = {"chat_id": chat_id, "action": action}
            if isinstance(message_thread_id, int):
                action_payload["message_thread_id"] = message_thread_id
            elif isinstance(message_thread_id, str) and message_thread_id.strip():
                action_payload["message_thread_id"] = int(message_thread_id)
            _post_telegram(
                self._api_base,
                self._token,
                "sendChatAction",
                action_payload,
                timeout=10.0,
            )
            return DeliveryResult(ok=True, meta={"chat_id": chat_id, "action": action})

        replace_remote_id = _optional_text(message.meta.get("replace_remote_id"))
        if replace_remote_id is not None:
            payload = {
                "chat_id": chat_id,
                "message_id": int(replace_remote_id),
                "text": message.text,
            }
            response = _post_telegram(
                self._api_base,
                self._token,
                "editMessageText",
                payload,
                timeout=10.0,
            )
            result = response.get("result")
            if not isinstance(result, dict):
                return DeliveryResult(ok=False, detail="telegram editMessageText returned no result")
            remote_id = result.get("message_id", replace_remote_id)
            return DeliveryResult(
                ok=True,
                remote_id=str(remote_id) if remote_id is not None else None,
                meta={"chat_id": chat_id, "mode": "edit"},
            )

        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": message.text,
        }
        if isinstance(message_thread_id, int):
            payload["message_thread_id"] = message_thread_id
        elif isinstance(message_thread_id, str) and message_thread_id.strip():
            payload["message_thread_id"] = int(message_thread_id)
        response = _post_telegram(
            self._api_base,
            self._token,
            "sendMessage",
            payload,
            timeout=10.0,
        )
        result = response.get("result")
        if not isinstance(result, dict):
            return DeliveryResult(ok=False, detail="telegram sendMessage returned no result")
        remote_id = result.get("message_id")
        return DeliveryResult(
            ok=True,
            remote_id=str(remote_id) if remote_id is not None else None,
            meta={"chat_id": chat_id},
        )

    def health(self, context: ChannelContext) -> PluginHealth:
        """Report current plugin health."""

        del context
        return PluginHealth(ok=True, meta={"api_base": self._api_base})

    def _delivery_from_message(
        self,
        message: dict[str, Any],
        *,
        update_id: int | None,
    ) -> InboundDelivery | None:
        chat = message.get("chat")
        if not isinstance(chat, dict):
            return None
        chat_id = _optional_text(chat.get("id"))
        if chat_id is None or not self._is_allowed_chat(chat_id):
            return None

        text = _optional_text(message.get("text")) or _optional_text(message.get("caption"))
        if text is None:
            return None

        sender = self._sender_for_chat(chat_id)
        message_thread_id = message.get("message_thread_id")
        thread_suffix = f":{message_thread_id}" if isinstance(message_thread_id, int) else ""
        thread_id = f"telegram:{chat_id}{thread_suffix}"
        reply_meta: dict[str, Any] = {"chat_id": chat_id}
        if isinstance(message_thread_id, int):
            reply_meta["message_thread_id"] = message_thread_id
        return InboundDelivery(
            origin="chat",
            channel="telegram",
            sender=sender,
            thread_id=thread_id,
            text=text,
            reply_target=ReplyTarget(
                channel="telegram",
                address=f"chat:{chat_id}",
                thread_id=thread_id,
                meta=reply_meta,
            ),
            meta={
                "chat_id": chat_id,
                "chat_type": _optional_text(chat.get("type")),
                "message_id": message.get("message_id"),
                "update_id": update_id,
            },
        )

    def _is_allowed_chat(self, chat_id: str) -> bool:
        allowed = set(self._allowed_chat_ids)
        if self._owner_chat_id is not None:
            allowed.add(self._owner_chat_id)
        allowed.update(self._peer_chat_ids)
        if not allowed:
            return True
        return chat_id in allowed

    def _sender_for_chat(self, chat_id: str) -> str:
        if self._owner_chat_id is not None and chat_id == self._owner_chat_id:
            return "owner"
        if chat_id in self._peer_chat_ids:
            return "peer"
        return "guest"


def create_channel(config: dict[str, Any]) -> ChannelPlugin:
    """Create one Telegram channel plugin instance."""

    return TelegramChannel(config=dict(config))


def _post_telegram(
    api_base: str,
    token: str,
    method: str,
    payload: dict[str, Any],
    *,
    timeout: float,
) -> dict[str, Any]:
    url = f"{api_base.rstrip('/')}/bot{token}/{method}"
    response = httpx.post(url, json=payload, timeout=timeout)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise ToolangError(f"telegram {method} returned a non-object response")
    if data.get("ok") is not True:
        description = _optional_text(data.get("description")) or "unknown error"
        raise ToolangError(f"telegram {method} failed: {description}")
    return data


def _reply_chat_id(target: ReplyTarget) -> str:
    chat_id = _optional_text(target.meta.get("chat_id"))
    if chat_id is not None:
        return chat_id
    if target.address.startswith("chat:"):
        stripped = target.address.removeprefix("chat:").strip()
        if stripped:
            return stripped
    raise ToolangError("telegram reply target is missing a chat id")


def _required_text(value: Any, *, name: str) -> str:
    text = _optional_text(value)
    if text is None:
        raise ToolangError(f"telegram channel config requires {name}")
    return text


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    text = _optional_text(value)
    if text is None:
        return None
    return int(text)


def _normalized_text_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [item for item in (_optional_text(part) for part in value.split(",")) if item]
    if isinstance(value, list | tuple):
        return [item for item in (_optional_text(part) for part in value) if item]
    return []
