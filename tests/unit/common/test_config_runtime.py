"""Plain runtime configuration patches keep source metadata separate."""

from dataclasses import replace
import json
from typing import Any, cast

import pytest

from toolang.common.config_runtime import RuntimeConfig
from toolang.common.config_sources import config_sources
from toolang.common.layout import AgentLayout
from toolang.setup.teaming import resolve_teaming_setup
from tests.support.layout import capture_guest_source


@pytest.mark.parametrize(
    "raw",
    [
        "secret",
        "[]",
        '{"model":null}',
        '{"root":{}}',
        '{"origins":{}}',
        '{"defaults":{}}',
        '{"workspaces":{"repo":{"source":"/host","path":"/guest","temporary":false}}}',
    ],
)
def test_invalid_patch_never_echoes_contents(raw):
    with pytest.raises(
        ValueError, match="invalid runtime configuration patch"
    ) as error:
        RuntimeConfig.parse(raw)
    assert "secret" not in str(error.value)


def test_plain_patch_overrides_configuration_without_changing_inputs(tmp_path):
    layout = AgentLayout.resident(tmp_path, "alice")
    layout.agent_home.mkdir(parents=True)
    layout.config.write_text("[limit]\ntokens=100\n")
    layout = replace(
        layout,
        runtime_config=RuntimeConfig.parse(
            '{"limit":{"tokens":200},"workspaces":{"repo":"/guest/repo"}}'
        ),
    )
    configs = config_sources(layout)
    assert all(source.config["limit"] == {"tokens": 200} for source in configs)
    assert configs[-1].config["workspaces"] == {"repo": "/guest/repo"}
    assert layout.config.read_text() == "[limit]\ntokens=100\n"


def test_source_defaults_do_not_freeze_explicit_configuration(tmp_path):
    host = AgentLayout.resident(tmp_path / "host", "alice")
    host.agent_home.mkdir(parents=True)
    guest = AgentLayout.resident(tmp_path / "guest", "alice")
    capture_guest_source(host, guest)
    data = json.loads(guest.root_source_receipt.read_text())
    data["launch"]["human"] = "host-owner"
    guest.root_source_receipt.write_text(json.dumps(data))

    def teaming():
        return resolve_teaming_setup(
            config_sources(guest),
            root=guest.root_config,
            home=guest.config,
            host_root=guest.host_root,
        )

    assert guest.host_root == host.host_root
    assert teaming().root.human == "human:host-owner"
    guest.root_config.write_text('[teaming]\nhuman="explicit-owner"\n')
    assert teaming().root.human == "human:explicit-owner"


def test_patch_is_deeply_immutable_and_returns_fresh_views():
    config = {"plugin": {"toolset": {"example": {"args": ["captured"]}}}}
    runtime = RuntimeConfig(config)
    config["plugin"]["toolset"]["example"]["args"].append("caller-only")
    with pytest.raises(TypeError):
        cast(dict[str, Any], runtime.values["plugin"])["toolset"]["example"]["args"][
            0
        ] = "changed"
    execution = runtime.apply({})
    cast(dict[str, Any], execution["plugin"])["toolset"]["example"]["args"].append(
        "execution-only"
    )
    assert runtime.apply({}) == {
        "plugin": {"toolset": {"example": {"args": ["captured"]}}}
    }


@pytest.mark.parametrize("keyed", [False, True])
def test_workspace_patch_uses_standard_flat_or_keyed_tables(tmp_path, keyed):
    paths = {"repo": "./repo"}
    patch = {"workspaces": {"alice": paths} if keyed else paths}
    runtime = RuntimeConfig.parse(json.dumps(patch))
    assert runtime.workspace_paths("alice", directory=tmp_path) == {
        "repo": tmp_path / "repo"
    }
