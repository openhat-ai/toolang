"""Host and guest share authored configuration and canonical State inputs."""

from pathlib import Path
from dataclasses import replace
import asyncio

import pytest

from toolang.common.config_runtime import (
    CONFIG_PATCH_ENV,
    RuntimeConfig,
    WorkspaceBinding,
)
from toolang.common.layout import AgentLayout
from toolang.state.prepare import prepare_agent_state, inspect_root_caps
from toolang.state.cache import layer_revision_dir
from toolang.state.watcher import StateWatcher
from toolang.up.mounts import prepare_place_mounts, prepare_guest_environment
from toolang.up.source import materialize_roaming_program

from tests.support.layout import root_file


def test_guest_mounts_original_configuration_bytes(tmp_path):
    layout = AgentLayout.resident(tmp_path / "host", "alice")
    layout.agent_home.mkdir(parents=True)
    layout.place_config.parent.mkdir(parents=True, exist_ok=True)
    layout.place_config.write_text(
        '# Preserve this source\n[workspaces.alice]\nrepo = "."\n'
    )
    layout.config.write_text('[workspaces]\nhome = "."\n')
    guest = Path("/guest")
    mounts = prepare_place_mounts(layout.agent_root, guest)
    root = next(m for m in mounts if m.hosted_path == guest / "config")
    assert root.local_path == layout.place_config.parent
    assert not root.read_only
    assert (
        root.local_path / "config.toml"
    ).read_bytes() == layout.place_config.read_bytes()
    assert not any(m.hosted_path == guest / "agents/alice/config.toml" for m in mounts)


def test_roaming_configuration_belongs_to_place(tmp_path):
    source = tmp_path / "aid.too"
    source.write_text("flow run():\n  pass\n")
    (tmp_path / "toolang.toml").write_text('[workspaces]\nrepo = "."\n')
    layout = materialize_roaming_program(source, toolang_root=tmp_path / "global")
    assert layout.place_config.is_file()
    assert not layout.config.exists()
    assert layout.place_config.samefile(tmp_path / "toolang.toml")


@pytest.mark.parametrize(
    "declaration", ["workspaces = 7", '[workspaces]\nrepo = "\\u0000"']
)
def test_roaming_guest_ignores_ancestor_workspace_declarations(tmp_path, declaration):
    import subprocess

    project = tmp_path / "project"
    project.mkdir()
    subprocess.run(["git", "init", "-q", str(project)], check=True)
    (project / "toolang.toml").write_text(declaration)
    source = project / "scripts" / "aid.too"
    source.parent.mkdir()
    source.write_text("flow run():\n  pass\n")
    layout = materialize_roaming_program(source, toolang_root=tmp_path / "global")
    assert prepare_agent_state(layout).workspaces == {}
    patch = prepare_guest_environment(layout, mapping={})[CONFIG_PATCH_ENV]
    assert "workspaces" not in RuntimeConfig.parse(patch).overlays.get("root", {})


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
    (root_file(root, "config.toml")).write_text('[teaming]\nhuman = "global-owner"\n')
    layout = materialize_roaming_program(source, toolang_root=root)
    authored = '[teaming]\nenabled = false\n[workspaces]\nnotes = "."\n'
    layout.config.write_text(authored)
    materialize_roaming_program(source, layout=layout)
    assert layout.config.read_text() == authored
    teaming = resolve_teaming_setup(
        config_sources(layout),
        root=layout.place_config,
        home=layout.config,
        host_root=layout.host_root,
    )
    assert teaming.root.human == "human:project-owner" and not teaming.enabled
    assert prepare_agent_state(layout).workspaces == {
        "notes": str(layout.agent_home),
        "repo": str(tmp_path),
    }


def test_roaming_workspace_commands_edit_authored_source(tmp_path):
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


@pytest.mark.parametrize("schema", ["0", "1"])
def test_incompatible_launch_schema_fails_before_state_writes(tmp_path, schema):
    from toolang.cli.toolang.main import main

    host = AgentLayout.resident(tmp_path, "aid")
    host.home.mkdir(parents=True)
    host.program.write_text("flow run():\n  pass\n")
    assert (
        main(["--root", str(tmp_path), "_serve", "aid", "--launch-schema", schema]) == 1
    )
    assert not (host.place / ".state").exists()
    assert not (host.home / ".state").exists()


@pytest.mark.parametrize("project", [False, True])
@pytest.mark.parametrize("linked", [False, True])
def test_shared_state_stays_stable_across_host_and_guest(tmp_path, project, linked):
    if project:
        source = tmp_path / "project/aid.too"
        source.parent.mkdir()
        source.write_text("flow run():\n  pass\n")
        project_path = "."
        if linked:
            (source.parent / "root-repo").mkdir()
            (source.parent / "root-link").symlink_to(
                source.parent / "root-repo", target_is_directory=True
            )
            project_path = "./root-link"
        (source.parent / "toolang.toml").write_text(
            f'[workspaces]\nproject = "{project_path}"\n'
        )
        host = materialize_roaming_program(source, toolang_root=tmp_path / "global")
    else:
        host = AgentLayout.resident(tmp_path / "host", "aid")
        host.home.mkdir(parents=True)
        host.place_config.parent.mkdir(parents=True, exist_ok=True)
        host.place_config.write_text('[workspaces.aid]\nproject = "."\n')
    repo = host.agent_home / "repo"
    repo.mkdir()
    if linked:
        (host.agent_home / "linked").symlink_to(repo, target_is_directory=True)
    host.config.write_text(
        f'[workspaces]\nrepo = "./{"linked" if linked else "repo"}"\n'
    )
    initial = prepare_agent_state(host)
    guest_place = tmp_path / "guest"
    guest_place.mkdir()
    for path in ("config", ".state"):
        (guest_place / path).symlink_to(host.agent_root / path)
    (guest_place / "agents").mkdir()
    (guest_place / "agents" / host.name).symlink_to(
        host.agent_home, target_is_directory=True
    )
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
    guest = replace(
        host,
        place=guest_place,
        root=guest_place,
        agent_root_path=guest_place,
        runtime_config=runtime,
    )
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


@pytest.mark.parametrize("scope", ["root", "home"])
def test_guest_does_not_restore_stale_symlink_identity_after_host_publication(
    tmp_path, scope
):
    from toolang.setup.types import AgentEnvironment, AgentSetup

    host = AgentLayout.resident(tmp_path / "host", "aid")
    host.home.mkdir(parents=True)
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    directory = host.place_config.parent if scope == "root" else host.home
    directory.mkdir(parents=True, exist_ok=True)
    link = directory / "repo-link"
    link.symlink_to(first, target_is_directory=True)
    config = host.place_config if scope == "root" else host.config
    table = "workspaces.aid" if scope == "root" else "workspaces"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(f'[{table}]\nrepo = "./repo-link"\n')
    if not host.place_config.exists():
        host.place_config.parent.mkdir(parents=True, exist_ok=True)
        host.place_config.write_text("")
    initial = prepare_agent_state(host)
    guest_place = tmp_path / "guest"
    guest_place.mkdir()
    for path in ("config", ".state"):
        (guest_place / path).symlink_to(host.agent_root / path)
    (guest_place / "agents").mkdir()
    (guest_place / "agents/aid").symlink_to(host.agent_home, target_is_directory=True)
    target = guest_place / "workspaces/aid/repo"
    runtime = RuntimeConfig.parse(
        prepare_guest_environment(host, mapping={"repo": (str(first), str(target))})[
            CONFIG_PATCH_ENV
        ],
        workspaces={"repo": WorkspaceBinding(first, target)},
    )
    guest = replace(
        host,
        place=guest_place,
        root=guest_place,
        agent_root_path=guest_place,
        runtime_config=runtime,
    )
    assert prepare_agent_state(guest).revision == initial.revision
    watcher = StateWatcher(guest, initial_state=initial)
    link.unlink()
    link.symlink_to(second, target_is_directory=True)
    changed = prepare_agent_state(host)
    assert changed.workspaces["repo"] == str(second)
    observed = asyncio.run(watcher._perform_check(requested=False))
    assert observed.failure is None
    assert observed.state.revision == changed.revision
    for _ in range(3):
        assert prepare_agent_state(guest).revision == changed.revision
        assert prepare_agent_state(host).revision == changed.revision
    assert (
        len(
            list(
                layer_revision_dir(host, "home", changed.home_revision).parent.iterdir()
            )
        )
        == 2
    )
    setup = AgentSetup(
        layout=guest,
        envs={},
        environment=AgentEnvironment.capture(guest, sandbox="docker:test", environ={}),
    )
    assert "repo" not in setup.workspace_roots(changed.workspaces)


@pytest.mark.parametrize("kind", ["packaged", "root", "home"])
def test_guest_catalog_selection_tracks_mounted_directory_inputs(tmp_path, kind):
    from toolang.plugin.catalogs.models_dev.path import resolve_model_catalog_selection

    host = AgentLayout.resident(tmp_path, "aid")
    host.home.mkdir(parents=True)
    if kind in {"root", "home"}:
        (root_file(host.agent_root, "catalog.json")).write_text("{}")
    if kind == "home":
        (host.home / "catalog.json").write_text("{}")
    selected = resolve_model_catalog_selection(host)
    patch = prepare_guest_environment(host, mapping={}, catalog=selected)[
        CONFIG_PATCH_ENV
    ]
    guest = replace(host, runtime_config=RuntimeConfig.parse(patch))
    assert resolve_model_catalog_selection(guest).path == selected.path
    if kind == "home":
        (host.home / "catalog.json").unlink()
        assert resolve_model_catalog_selection(guest).scope == "root"
    else:
        (host.home / "catalog.json").write_text("{}")
        assert resolve_model_catalog_selection(guest).path == host.home / "catalog.json"
