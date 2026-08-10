from __future__ import annotations

import tomllib

import pytest

from toolang.plugin.config import merge_named_configs, merge_sandbox_config


def test_merge_named_configs_merges_root_and_agent_sections() -> None:
    root_config = tomllib.loads(
        """
[tools.working_tree]
root = "/global"

[channels.telegram]
plugin = "telegram"
token_env = "TELEGRAM_BOT_TOKEN"
owner_chat_id = "100"
""".strip()
    )
    home_config = tomllib.loads(
        """
[tools.working_tree]
root = "/agent"

[channels.telegram]
owner_chat_id = "123"
""".strip()
    )

    tools = merge_named_configs((root_config, home_config), section="tools", environ={})
    channels = merge_named_configs(
        (root_config, home_config),
        section="channels",
        environ={"TELEGRAM_BOT_TOKEN": "secret"},
    )

    assert tools == {"working_tree": {"root": "/agent"}}
    assert channels == {
        "telegram": {
            "plugin": "telegram",
            "token": "secret",
            "owner_chat_id": "123",
        }
    }


def test_merge_named_configs_reports_missing_environment_reference() -> None:
    root_config = tomllib.loads(
        """
[channels.telegram]
plugin = "telegram"
token_env = "TELEGRAM_BOT_TOKEN"
""".strip()
    )

    with pytest.raises(
        ValueError,
        match="channels.telegram.token_env.*TELEGRAM_BOT_TOKEN",
    ):
        merge_named_configs((root_config,), section="channels", environ={})


def test_merge_sandbox_config_merges_root_and_agent_sections() -> None:
    root_config = tomllib.loads(
        """
[sandbox]
driver = "docker"
target = "python:3.13"

[sandbox.config]
token_env = "SANDBOX_TOKEN"
""".strip()
    )
    home_config = tomllib.loads(
        """
[sandbox.config]
image = "python:3.13-slim"
""".strip()
    )

    config = merge_sandbox_config(
        (root_config, home_config),
        environ={"SANDBOX_TOKEN": "secret"},
    )

    assert config == {
        "driver": "docker",
        "target": "python:3.13",
        "config": {
            "image": "python:3.13-slim",
            "token": "secret",
        },
    }


def test_merge_sandbox_config_reports_missing_environment_reference() -> None:
    root_config = tomllib.loads(
        """
[sandbox]
driver = "docker"

[sandbox.config]
token_env = "SANDBOX_TOKEN"
""".strip()
    )

    with pytest.raises(
        ValueError,
        match="sandbox.config.token_env.*SANDBOX_TOKEN",
    ):
        merge_sandbox_config((root_config,), environ={})
