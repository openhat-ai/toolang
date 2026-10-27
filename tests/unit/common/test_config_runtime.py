"""Launch corrections preserve authored policy and confidential values."""

from dataclasses import replace
import json
from pathlib import Path
from typing import Any, cast

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
            config_sources(layout),
            root=layout.place_config,
            home=layout.config,
            host_root=layout.host_root,
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


def test_guest_teaming_keeps_host_root_separate_from_mounted_root_and_place(tmp_path):
    from toolang.up.mounts import prepare_guest_environment

    host = AgentLayout(
        tmp_path / "project", "alice", "roaming", root=tmp_path / "selected-root"
    )
    patch = prepare_guest_environment(host, mapping={})["TOOLANG_CONFIG_PATCH"]
    guest = AgentLayout(
        Path("/guest/project"),
        "alice",
        "roaming",
        root=Path("/guest/toolang"),
        runtime_config=RuntimeConfig.parse(patch),
    )
    assert guest.host_root == host.root
    assert guest.host_root != guest.root and guest.host_root != guest.place
    teaming = resolve_teaming_setup(
        (),
        root=guest.place_config,
        home=guest.config,
        host_root=guest.host_root,
        default_human="host-owner",
    )
    assert teaming.connection().root == str(host.root)
    assert teaming.toolset_config()["root"] == str(host.root)


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


@pytest.mark.parametrize("section", ["defaults", "overlays"])
def test_captured_patch_cannot_be_changed_through_nested_values(section):
    runtime = RuntimeConfig.parse(
        '{"defaults":{"root":{"teaming":{"human":"owner"}}},'
        '"home":{"plugin":{"toolset":{"example":{"args":["captured"]}}}}}'
    )
    values = getattr(runtime, section)
    if section == "defaults":
        with pytest.raises(TypeError):
            values["root"]["teaming"]["human"] = "changed"
    else:
        with pytest.raises(TypeError):
            values["home"]["plugin"]["toolset"]["example"]["args"][0] = "changed"
    execution = runtime.apply("home", {})
    plugin = cast(dict[str, Any], execution["plugin"])
    plugin["toolset"]["example"]["args"].append("execution-only")
    assert runtime.apply("home", {}) == {
        "plugin": {"toolset": {"example": {"args": ["captured"]}}}
    }


def test_captured_patch_does_not_retain_mutable_caller_configuration():
    config = {"teaming": {"human": "owner"}}
    runtime = RuntimeConfig(defaults={"root": config})
    config["teaming"]["human"] = "changed"
    assert runtime.apply("root", {}) == {"teaming": {"human": "owner"}}


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
