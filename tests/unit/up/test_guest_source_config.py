"""Host and guest share authored configuration and canonical State inputs."""

from pathlib import Path
from dataclasses import replace

import pytest

from toolang.common.config_runtime import (
    CONFIG_PATCH_ENV,
    RuntimeConfig,
    WorkspaceBinding,
)
from toolang.common.layout import AgentLayout
from toolang.state.prepare import prepare_agent_state, inspect_root_caps
from toolang.state.cache import layer_revision_dir
from toolang.up.mounts import prepare_source_mounts, prepare_guest_environment
from toolang.up.source import materialize_roaming_program


def test_guest_mounts_original_configuration_bytes(tmp_path):
    layout = AgentLayout.resident(tmp_path / "host", "alice")
    layout.home.mkdir(parents=True)
    layout.place_config.write_text(
        '# Preserve this source\n[workspaces.alice]\nrepo = "."\n'
    )
    layout.config.write_text('[workspaces]\nhome = "."\n')
    guest = Path("/guest")
    mounts = prepare_source_mounts(layout.place, layout.name, guest)
    root = next(m for m in mounts if m.hosted_path == guest / "config.toml")
    assert root.local_path == layout.place_config
    assert root.read_only
    assert root.local_path.read_bytes() == layout.place_config.read_bytes()
    assert not any(m.hosted_path == guest / "agents/alice/config.toml" for m in mounts)


def test_roaming_configuration_belongs_to_place(tmp_path):
    source = tmp_path / "aid.too"
    source.write_text("flow run():\n  pass\n")
    (tmp_path / "toolang.toml").write_text('[workspaces]\nrepo = "."\n')
    layout = materialize_roaming_program(source, toolang_root=tmp_path / "global")
    assert layout.place_config.is_file()
    assert not layout.config.exists()
    assert "__toolang_sources__" in layout.place_config.read_text()


@pytest.mark.parametrize("legacy_link", [False, True])
def test_roaming_relocates_only_owned_legacy_home_inputs(tmp_path, legacy_link):
    from toolang.common.config_sources import project_sources, script_sources

    source = tmp_path / "aid.too"
    source.write_text("flow run():\n  pass\n")
    config = tmp_path / "toolang.toml"
    config.write_text('[workspaces]\nrepo = "."\n')
    catalog = tmp_path / "toolang.catalog.json"
    catalog.write_text("{}")
    layout = materialize_roaming_program(source, toolang_root=tmp_path / "global")
    layout.place_config.unlink()
    if legacy_link:
        layout.config.symlink_to(config)
    else:
        layout.config.write_text(
            project_sources(script_sources(source), source=source, catalog=catalog)
        )
    (layout.home / "catalog.json").symlink_to(catalog)
    materialize_roaming_program(source, layout=layout)
    assert layout.place_config.is_file()
    assert not layout.config.exists() and not layout.config.is_symlink()
    assert not (layout.home / "catalog.json").exists()
    assert config.read_text() == '[workspaces]\nrepo = "."\n'


def test_roaming_preserves_authored_home_and_ignores_global_agent_config(tmp_path):
    from toolang.common.config_sources import config_sources
    from toolang.setup.teaming import resolve_teaming_setup

    source = tmp_path / "aid.too"
    source.write_text("flow run():\n  pass\n")
    (tmp_path / "toolang.toml").write_text(
        '[teaming]\nhuman = "project-owner"\n[workspaces]\nrepo = "."\n'
    )
    root = tmp_path / "global"
    root.mkdir()
    (root / "config.toml").write_text('[teaming]\nhuman = "global-owner"\n')
    layout = materialize_roaming_program(source, toolang_root=root)
    authored = '[teaming]\nenabled = false\n[workspaces]\nnotes = "."\n'
    layout.config.write_text(authored)
    materialize_roaming_program(source, layout=layout)
    assert layout.config.read_text() == authored
    teaming = resolve_teaming_setup(
        config_sources(layout), root=layout.place_config, home=layout.config
    )
    assert teaming.root.human == "human:project-owner" and not teaming.enabled
    assert prepare_agent_state(layout).workspaces == {
        "notes": str(layout.home),
        "repo": str(tmp_path),
    }


def test_roaming_workspace_commands_edit_authored_root_instead_of_projection(tmp_path):
    import tomllib
    from toolang.cli.toolang.main import main

    source = tmp_path / "aid.too"
    source.write_text("flow run():\n  pass\n")
    root = tmp_path / "global"
    arguments = ["--root", str(root), str(source), "workspace"]
    assert main([*arguments, "add", str(tmp_path), "--name", "repo"]) == 0
    assert main([*arguments, "remove", "repo"]) == 0
    assert tomllib.loads((tmp_path / "toolang.toml").read_text())["workspaces"] == {}
    assert source.is_file()


def test_incompatible_launch_schema_fails_before_state_writes(tmp_path):
    from toolang.cli.toolang.main import main

    host = AgentLayout.resident(tmp_path, "aid")
    host.home.mkdir(parents=True)
    host.program.write_text("flow run():\n  pass\n")
    assert main(["--root", str(tmp_path), "_serve", "aid", "--launch-schema", "0"]) == 1
    assert not (host.place / ".state").exists()
    assert not (host.home / ".state").exists()


@pytest.mark.parametrize("project", [False, True])
@pytest.mark.parametrize("linked", [False, True])
def test_shared_state_stays_stable_across_host_and_guest(tmp_path, project, linked):
    if project:
        source = tmp_path / "project/aid.too"
        source.parent.mkdir()
        source.write_text("flow run():\n  pass\n")
        (source.parent / "toolang.toml").write_text('[workspaces]\nproject = "."\n')
        host = materialize_roaming_program(source, toolang_root=tmp_path / "global")
    else:
        host = AgentLayout.resident(tmp_path / "host", "aid")
        host.home.mkdir(parents=True)
        host.place_config.write_text('[workspaces.aid]\nproject = "."\n')
    repo = host.home / "repo"
    repo.mkdir()
    if linked:
        (host.home / "linked").symlink_to(repo, target_is_directory=True)
    host.config.write_text(
        f'[workspaces]\nrepo = "./{"linked" if linked else "repo"}"\n'
    )
    initial = prepare_agent_state(host)
    guest_place = tmp_path / "guest"
    guest_place.mkdir()
    for path in ("config.toml", ".state"):
        (guest_place / path).symlink_to(host.place / path)
    (guest_place / "agents").mkdir()
    (guest_place / "agents" / host.name).symlink_to(host.home, target_is_directory=True)
    mapping = {
        name: (value, str(guest_place / "workspaces" / host.name / name))
        for name, value in initial.workspaces.items()
    }
    patch = prepare_guest_environment(host, mapping=mapping)[CONFIG_PATCH_ENV]
    runtime = RuntimeConfig.parse(
        patch,
        workspaces={
            name: WorkspaceBinding(Path(source), Path(path))
            for name, (source, path) in mapping.items()
        },
    )
    guest = replace(host, place=guest_place, root=guest_place, runtime_config=runtime)
    for _ in range(3):
        assert prepare_agent_state(guest).revision == initial.revision
        inspect_root_caps(host, kinds=set())
        assert prepare_agent_state(host).revision == initial.revision
    assert (
        len(
            list(
                layer_revision_dir(host, "root", initial.root_revision).parent.iterdir()
            )
        )
        == 1
    )
    assert (
        len(
            list(
                layer_revision_dir(host, "home", initial.home_revision).parent.iterdir()
            )
        )
        == 1
    )
    host.config.write_text(host.config.read_text() + "[teaming]\nenabled = false\n")
    updated = prepare_agent_state(guest)
    assert updated.revision != initial.revision
    assert prepare_agent_state(host).revision == updated.revision
    assert updated.workspaces == initial.workspaces
