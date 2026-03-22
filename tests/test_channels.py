from __future__ import annotations

import json
from pathlib import Path

from toolang.channels import create_channel_plugin, decode_hook_delivery, find_hook_binding
from toolang.concepts.persisted import (
    ChannelBinding,
    ChannelsConfig,
    HookBinding,
    HooksConfig,
)


def test_channels_config_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "channels.toml"
    config = ChannelsConfig(
        channels={
            "telegram": ChannelBinding(
                plugin="telegram",
                config={"token_env": "TELEGRAM_BOT_TOKEN", "owner_chat_id": "123"},
            )
        }
    )

    config.save(path)
    loaded = ChannelsConfig.load(path)

    assert loaded == config
    assert "[channels.telegram]" in path.read_text(encoding="utf-8")


def test_hooks_config_roundtrip_normalizes_method(tmp_path: Path) -> None:
    path = tmp_path / "hooks.toml"
    config = HooksConfig(
        hooks={
            "github_push": HookBinding(
                path="/hooks/github/push",
                plugin="webhook",
                method="post",
                idempotency_header="X-GitHub-Delivery",
                config={"origin": "invoke"},
            )
        }
    )

    config.save(path)
    loaded = HooksConfig.load(path)

    binding = loaded.hooks["github_push"]
    assert binding.method == "POST"
    assert loaded == HooksConfig(
        hooks={
            "github_push": HookBinding(
                path="/hooks/github/push",
                plugin="webhook",
                method="POST",
                idempotency_header="X-GitHub-Delivery",
                config={"origin": "invoke"},
            )
        }
    )


def test_find_hook_binding_matches_path_and_method() -> None:
    config = HooksConfig(
        hooks={
            "incoming": HookBinding(
                path="/hooks/incoming",
                plugin="webhook",
                method="POST",
            )
        }
    )

    matched = find_hook_binding(config, path="/hooks/incoming", method="post")

    assert matched is not None
    name, binding = matched
    assert name == "incoming"
    assert binding.plugin == "webhook"


def test_decode_hook_delivery_uses_builtin_webhook_plugin() -> None:
    config = HooksConfig(
        hooks={
            "incoming": HookBinding(
                path="/hooks/incoming",
                plugin="webhook",
                method="POST",
                config={"origin": "invoke", "sender": "service"},
            )
        }
    )

    match = decode_hook_delivery(
        config,
        path="/hooks/incoming",
        method="POST",
        headers={"content-type": "application/json"},
        query={},
        body=json.dumps(
            {
                "thread_id": "repo:toolang",
                "text": "Refresh the release branch.",
                "meta": {"source": "github"},
                "reply_target": {
                    "channel": "telegram",
                    "address": "chat:123",
                    "thread_id": "telegram:123",
                },
            }
        ).encode("utf-8"),
        content_type="application/json",
    )

    assert match is not None
    assert match.name == "incoming"
    assert match.delivery.origin == "invoke"
    assert match.delivery.sender == "service"
    assert match.delivery.thread_id == "repo:toolang"
    assert match.delivery.text == "Refresh the release branch."
    assert match.delivery.meta == {"source": "github"}
    assert match.delivery.reply_target is not None
    assert match.delivery.reply_target.channel == "telegram"
    assert match.delivery.reply_target.address == "chat:123"


def test_create_builtin_webhook_plugin_health() -> None:
    plugin = create_channel_plugin("webhook")

    assert plugin.health().ok is True
