from __future__ import annotations

import tomllib

import pytest

from toolang.plugin.config import (
    merge_plugin_configs,
    resolve_sandbox_binding,
)
from toolang.plugin.models.config import parse_provider_configs


def test_merge_plugin_configs_deeply_merges_root_and_agent_layers() -> None:
    root_config = tomllib.loads(
        """
[plugin.toolset.fs]
root = "/global"

[plugin.toolset.fs.options]
hidden = false
limit = 10
""".strip()
    )
    agent_config = tomllib.loads(
        """
[plugin.toolset.fs]
root = "/agent"

[plugin.toolset.fs.options]
limit = 20
""".strip()
    )

    toolsets = merge_plugin_configs(
        (root_config, agent_config),
        family="toolset",
    )

    assert toolsets == {
        "fs": {
            "root": "/agent",
            "options": {"hidden": False, "limit": 20},
        }
    }


def test_merge_plugin_configs_passes_environment_variable_names_unchanged() -> None:
    config = tomllib.loads(
        """
[plugin.model_catalog.company]
credential_env = "COMPANY_CATALOG_TOKEN"
""".strip()
    )

    catalogs = merge_plugin_configs((config,), family="model_catalog")

    assert catalogs == {"company": {"credential_env": "COMPANY_CATALOG_TOKEN"}}


@pytest.mark.parametrize(
    ("source", "message"),
    [
        ("[tools.fs]", "unsupported plugin config section: tools"),
        ("[channels.telegram]", "unsupported plugin config section: channels"),
        (
            "[sandbox]\ndriver = 'docker'\n[sandbox.config]",
            "unknown sandbox config field: config",
        ),
        (
            "[plugin.toolsets.fs]",
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
        merge_plugin_configs((config,), family="toolset")


def test_removed_model_catalog_config_fails_in_model_config_parser() -> None:
    config = tomllib.loads("[models.catalogs.company]")

    with pytest.raises(ValueError, match="unknown models config field: catalogs"):
        parse_provider_configs((config,))


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
