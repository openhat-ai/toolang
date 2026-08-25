from __future__ import annotations

from pathlib import Path

from toolang.base.protocols.channel import AgentChannel
from toolang.base.types.channel import (
    ChannelContext,
    ChannelState,
    OutboundMessage,
    ReplyTarget,
)
from toolang.plugin.config import parse_channel_bindings
from toolang.plugin.channels.loading import create_channel


def _channel_context(home: Path, binding_name: str) -> ChannelContext:
    return ChannelContext(
        home=home,
        room=home / ".runtime" / "channels" / binding_name,
    )


def test_create_experiments_telegram_channel_plugin() -> None:
    plugin = create_channel(
        "telegram",
        config={"token": "secret", "owner_chat_id": "123"},
    )

    assert isinstance(plugin, AgentChannel)
    assert plugin.health(_channel_context(Path("/tmp/alice"), "telegram")).ok is True


def test_telegram_channel_polls_and_delivers(monkeypatch) -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    class FakeResponse:
        def __init__(self, payload: dict[str, object]) -> None:
            self._payload = payload

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return self._payload

    def fake_post(url: str, *, json: dict[str, object], timeout: float):
        calls.append((url, dict(json)))
        if url.endswith("/getUpdates"):
            return FakeResponse(
                {
                    "ok": True,
                    "result": [
                        {
                            "update_id": 41,
                            "message": {
                                "message_id": 7,
                                "text": "hello from telegram",
                                "chat": {"id": 123, "type": "private"},
                            },
                        }
                    ],
                }
            )
        return FakeResponse({"ok": True, "result": {"message_id": 88}})

    monkeypatch.setattr("toolang.plugin.channels.telegram.httpx.post", fake_post)
    plugin = create_channel(
        "telegram",
        config={"token": "secret", "owner_chat_id": "123"},
    )
    context = _channel_context(Path("/tmp/alice"), "telegram")

    polled = plugin.poll(ChannelState(cursor="40"), context)

    assert len(polled.deliveries) == 1
    delivery = polled.deliveries[0]
    assert delivery.origin == "chat"
    assert delivery.sender == "owner"
    assert delivery.channel == "telegram"
    assert delivery.thread_id == "script_tg_123"
    assert delivery.reply_target is not None
    assert delivery.reply_target.address == "chat:123"
    assert polled.next_state.cursor == "42"

    delivered = plugin.deliver(
        delivery.reply_target, OutboundMessage(text="hi back"), context
    )

    assert delivered.ok is True
    assert delivered.remote_id == "88"
    assert calls[0][0].endswith("/getUpdates")
    assert calls[0][1]["offset"] == 40
    assert calls[1][0].endswith("/sendMessage")
    assert calls[1][1]["chat_id"] == "123"
    assert calls[1][1]["text"] == "hi back"


def test_telegram_channel_typing_and_edit(monkeypatch) -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    class FakeResponse:
        def __init__(self, payload: dict[str, object]) -> None:
            self._payload = payload

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return self._payload

    def fake_post(url: str, *, json: dict[str, object], timeout: float):
        calls.append((url, dict(json)))
        return FakeResponse({"ok": True, "result": {"message_id": 88}})

    monkeypatch.setattr("toolang.plugin.channels.telegram.httpx.post", fake_post)
    plugin = create_channel(
        "telegram",
        config={"token": "secret", "owner_chat_id": "123"},
    )
    context = _channel_context(Path("/tmp/alice"), "telegram")

    target = ReplyTarget(
        channel="telegram", address="chat:123", meta={"chat_id": "123"}
    )
    typing = plugin.deliver(
        target, OutboundMessage(text="", meta={"action": "typing"}), context
    )
    sent = plugin.deliver(target, OutboundMessage(text="hello"), context)
    edited = plugin.deliver(
        target,
        OutboundMessage(text="hello world", meta={"replace_remote_id": "88"}),
        context,
    )

    assert typing.ok is True
    assert sent.ok is True
    assert edited.ok is True
    assert calls[0][0].endswith("/sendChatAction")
    assert calls[0][1]["action"] == "typing"
    assert calls[1][0].endswith("/sendMessage")
    assert calls[1][1]["text"] == "hello"
    assert calls[2][0].endswith("/editMessageText")
    assert calls[2][1]["message_id"] == 88
    assert calls[2][1]["text"] == "hello world"


def test_parse_channel_bindings_builds_plugin_specific_config() -> None:
    bindings = parse_channel_bindings(
        {
            "telegram": {
                "token": "secret",
                "owner_chat_id": "123",
            }
        }
    )

    assert bindings["telegram"].name == "telegram"
    assert bindings["telegram"].config == {
        "token": "secret",
        "owner_chat_id": "123",
    }
