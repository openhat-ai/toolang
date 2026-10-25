"""Launch corrections preserve authored policy and confidential values."""

from dataclasses import replace
import json
from pathlib import Path

import pytest

from toolang.common.config_runtime import (
    RuntimeConfig,
    WorkspaceBinding,
    parse_workspace_bindings,
)
from toolang.common.config_sources import config_sources
from toolang.common.layout import AgentLayout
from toolang.setup.teaming import resolve_teaming_setup


@pytest.mark.parametrize(
    "raw",
    [
        "secret",
        "[]",
        '{"secret":"credential"}',
        '{"root":null}',
        '{"defaults":{"root":{"teaming":{"human":null}}}}',
        '{"home":{"allow":{"tools":["*"]}}}',
        '{"origins":{"home":"relative"}}',
    ],
)
def test_invalid_patch_never_echoes_contents(raw):
    with pytest.raises(ValueError) as error:
        RuntimeConfig.parse(raw)
    assert str(error.value) == "invalid runtime configuration patch"


def test_runtime_defaults_do_not_freeze_explicit_configuration(tmp_path):
    layout = AgentLayout.resident(tmp_path, "alice")
    layout.home.mkdir(parents=True)
    runtime = RuntimeConfig.parse(
        json.dumps({"defaults": {"root": {"teaming": {"human": "host-owner"}}}})
    )
    layout = replace(layout, runtime_config=runtime)

    def teaming():
        return resolve_teaming_setup(
            config_sources(layout), root=layout.place_config, home=layout.config
        )

    assert teaming().root.human == "human:host-owner"
    layout.place_config.write_text(
        '[teaming]\nhuman = "explicit-owner"\nenabled = false\nhub = "http://new-host:7001"\n'
    )
    assert teaming().root.human == "human:explicit-owner"
    assert not teaming().enabled
    assert teaming().root.hub == "http://new-host:7001"
    layout.place_config.write_text("")
    assert teaming().root.human == "human:host-owner"


def test_workspace_patch_cannot_restore_revoked_or_rebound_grants():
    binding = WorkspaceBinding(Path("/host/repo"), Path("/guest/repo"))
    runtime = RuntimeConfig.parse(
        '{"home":{"workspaces":{"repo":"/guest/repo"}}}', workspaces={"repo": binding}
    )
    original = {"workspaces": {"repo": "/host/repo"}}
    assert runtime.apply("home", original)["workspaces"] == {"repo": "/guest/repo"}
    assert original == {"workspaces": {"repo": "/host/repo"}}
    assert runtime.apply("home", {"workspaces": {}})["workspaces"] == {}
    assert runtime.apply("home", {"workspaces": {"repo": "/host/rebound"}})[
        "workspaces"
    ] == {"repo": "/host/rebound"}


def test_workspace_patch_rejects_unmounted_targets_before_startup():
    binding = WorkspaceBinding(Path("/host/repo"), Path("/guest/repo"))
    with pytest.raises(ValueError, match="invalid runtime configuration patch"):
        RuntimeConfig.parse(
            '{"home":{"workspaces":{"repo":"/guest/different"}}}',
            workspaces={"repo": binding},
        )


@pytest.mark.parametrize(
    "raw",
    [
        '{"repo":"/old-shape"}',
        '{"lab":{"source":"/host","path":"/guest","temporary":true}}',
        '{"repo":{"source":"relative","path":"/guest","temporary":false}}',
    ],
)
def test_incompatible_workspace_startup_records_are_rejected(raw):
    with pytest.raises(ValueError, match="invalid workspace bindings startup input"):
        parse_workspace_bindings(raw)
