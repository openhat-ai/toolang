from __future__ import annotations

import tomllib

import pytest

from toolang.plugin.config import (
    merge_plugin_configs,
    resolve_sandbox_binding,
)


def test_merge_plugin_configs_deeply_merges_root_and_agent_layers() -> None:
    root_config = tomllib.loads(
        """
[plugin.toolset.filesystem]
root = "/global"

[plugin.toolset.filesystem.options]
hidden = false
limit = 10

[plugin.channel.telegram]
token_env = "TELEGRAM_BOT_TOKEN"
owner_chat_id = "100"
""".strip()
    )
    agent_config = tomllib.loads(
        """
[plugin.toolset.filesystem]
root = "/agent"

[plugin.toolset.filesystem.options]
limit = 20

[plugin.channel.telegram]
owner_chat_id = "123"
""".strip()
    )

    toolsets = merge_plugin_configs(
        (root_config, agent_config),
        family="toolset",
        environ={},
    )
    channels = merge_plugin_configs(
        (root_config, agent_config),
        family="channel",
        environ={"TELEGRAM_BOT_TOKEN": "secret"},
    )

    assert toolsets == {
        "filesystem": {
            "root": "/agent",
            "options": {"hidden": False, "limit": 20},
        }
    }
    assert channels == {"telegram": {"token": "secret", "owner_chat_id": "123"}}


def test_merge_plugin_configs_resolves_nested_environment_references() -> None:
    config = tomllib.loads(
        """
[plugin.model_adapter.responses]

[plugin.model_adapter.responses.headers]
authorization_env = "MODEL_TOKEN"
""".strip()
    )

    adapters = merge_plugin_configs(
        (config,),
        family="model_adapter",
        environ={"MODEL_TOKEN": "secret"},
    )

    assert adapters == {"responses": {"headers": {"authorization": "secret"}}}


def test_direct_plugin_value_takes_precedence_over_environment_reference() -> None:
    config = tomllib.loads(
        """
[plugin.channel.telegram]
token = "configured"
token_env = "MISSING_TOKEN"
""".strip()
    )

    channels = merge_plugin_configs((config,), family="channel", environ={})

    assert channels == {"telegram": {"token": "configured"}}


def test_merge_plugin_configs_reports_missing_environment_reference() -> None:
    config = tomllib.loads(
        """
[plugin.channel.telegram]
token_env = "TELEGRAM_BOT_TOKEN"
""".strip()
    )

    with pytest.raises(
        ValueError,
        match=(r"plugin\.channel\.telegram\.token_env.*TELEGRAM_BOT_TOKEN"),
    ):
        merge_plugin_configs((config,), family="channel", environ={})


@pytest.mark.parametrize(
    ("source", "message"),
    [
        ("[tools.filesystem]", "unsupported plugin config section: tools"),
        ("[channels.telegram]", "unsupported plugin config section: channels"),
        (
            "[sandbox]\ndriver = 'docker'\n[sandbox.config]",
            "unknown sandbox config field: config",
        ),
        (
            "[models.catalogs.company]",
            "unknown models config field: catalogs",
        ),
        (
            "[plugin.toolsets.filesystem]",
            "unknown plugin config field: toolsets",
        ),
    ],
)
def test_removed_and_unknown_plugin_config_shapes_fail(
    source: str,
    message: str,
) -> None:
    config = tomllib.loads(source)

    with pytest.raises(ValueError, match=message):
        merge_plugin_configs((config,), family="toolset", environ={})


def test_resolve_sandbox_binding_layers_driver_and_target() -> None:
    root_config = tomllib.loads(
        """
[sandbox]
driver = "docker"
target = "python:3.13"
""".strip()
    )
    agent_config = tomllib.loads(
        """
[sandbox]
target = "python:3.13-slim"
""".strip()
    )

    binding = resolve_sandbox_binding((root_config, agent_config))

    assert binding is not None
    assert binding.name == "docker"
    assert binding.spec == "python:3.13-slim"
