from __future__ import annotations

from pathlib import Path
import json
import pwd
from types import SimpleNamespace

from toolang.common.layout import AgentLayout
from toolang.state.prepare import prepare_agent_state

import pytest

from toolang.base.types.sandbox import SandboxMount
from toolang.up.mounts import (
    prepare_catalog_mount,
    prepare_guest_environment,
    prepare_place_mounts,
    prepare_workspace_mounts,
    validate_source_mounts,
)

from tests.support.layout import root_file


@pytest.mark.parametrize("resident", [False, True])
def test_guest_environment_keeps_configuration_out_of_state(
    tmp_path, monkeypatch, resident
):
    root = tmp_path / "root"
    place = root if resident else tmp_path / "project"
    layout = AgentLayout(
        place, "alice", "resident" if resident else "visiting", root=root
    )
    layout.home.mkdir(parents=True)
    authored = '[teaming]\nhuman = "explicit"\nhub = "http://first"\n'
    layout.place_config.parent.mkdir(parents=True, exist_ok=True)
    layout.place_config.write_text(authored)
    monkeypatch.setenv("LOGNAME", "bryan")
    monkeypatch.setenv("USER", "host-human")
    monkeypatch.setattr(
        pwd, "getpwuid", lambda uid: SimpleNamespace(pw_name="host-human")
    )
    patch = json.loads(
        prepare_guest_environment(layout, mapping={})["TOOLANG_CONFIG_PATCH"]
    )
    assert patch["defaults"] == {"root": {"teaming": {"human": "host-human"}}}
    assert patch["origins"]["root"] == str(layout.place_config)
    assert "root" not in patch and "home" not in patch
    assert layout.place_config.read_text() == authored


def test_prepare_place_mounts_owns_place_inputs(tmp_path: Path) -> None:
    local_place = tmp_path / "toolang"
    hosted_place = Path("/root/.toolang")

    mounts = prepare_place_mounts(local_place, hosted_place)

    assert {(item.local_path, item.hosted_path) for item in mounts} == {
        (local_place / "config", hosted_place / "config"),
        (local_place / ".state/root", hosted_place / ".state/root"),
        (local_place / "psyches", hosted_place / "psyches"),
        (local_place / "skills", hosted_place / "skills"),
        (local_place / "services", hosted_place / "services"),
        (local_place / "prompts", hosted_place / "prompts"),
    }
    assert (local_place / "config").is_dir()
    assert not (local_place / ".setup").exists()
    assert all(
        (local_place / name).is_dir()
        for name in (
            ".state",
            "psyches",
            "skills",
            "services",
            "prompts",
        )
    )


def test_place_catalog_is_covered_by_writable_directory_mount(tmp_path: Path) -> None:
    local = tmp_path / "root"
    catalog = root_file(local, "catalog.json")
    catalog.write_text("{}")
    guest = Path("/root/.toolang")
    mounts = prepare_place_mounts(local, guest)
    assert SandboxMount(local / "config", guest / "config") in mounts
    assert all(mount.local_path.is_dir() for mount in mounts)
    assert all(not mount.read_only for mount in mounts)


@pytest.mark.parametrize("relative", [False, True])
def test_source_links_must_resolve_in_the_guest_directory_view(tmp_path, relative):
    layout = AgentLayout.resident(tmp_path, "alice")
    layout.agent_home.mkdir(parents=True)
    target = layout.agent_home / "entry-source"
    target.write_text("flow run():\n  pass\n")
    layout.program.symlink_to(target.name if relative else target)
    mounts = (SandboxMount(layout.agent_home, Path("/guest/agents/alice")),)
    if relative:
        validate_source_mounts(layout, mounts)
    else:
        with pytest.raises(ValueError, match="source symlink target is unavailable"):
            validate_source_mounts(layout, mounts)


@pytest.mark.parametrize(
    "source_kind",
    ["program", "root-catalog", "home-catalog", "root-receipt", "home-receipt"],
)
def test_source_link_chain_cannot_escape_the_guest_directory_view(
    tmp_path, source_kind
):
    layout = AgentLayout.resident(tmp_path, "alice")
    layout.agent_home.mkdir(parents=True)
    source = {
        "program": layout.program,
        "root-catalog": layout.root_catalog,
        "home-catalog": layout.agent_home / "catalog.json",
        "root-receipt": layout.root_source_receipt,
        "home-receipt": layout.source_receipt,
    }[source_kind]
    source.parent.mkdir(parents=True, exist_ok=True)
    target = tmp_path / "outside.too"
    target.write_text("flow run():\n  pass\n")
    (source.parent / "intermediate").symlink_to(target)
    source.symlink_to("intermediate")
    guest = Path("/guest")
    mounts = (
        SandboxMount(layout.agent_home, guest / "agents/alice"),
        *prepare_place_mounts(layout.agent_root, guest),
    )
    with pytest.raises(ValueError, match="source symlink target is unavailable"):
        validate_source_mounts(layout, mounts)


@pytest.mark.parametrize("through_directory", [False, True])
def test_source_link_chain_can_resolve_inside_the_guest_mount(
    tmp_path, through_directory
):
    layout = AgentLayout.resident(tmp_path, "alice")
    directory = layout.agent_home / "entries"
    directory.mkdir(parents=True)
    target = directory / "source.too"
    target.write_text("flow run():\n  pass\n")
    (layout.agent_home / "intermediate").symlink_to("entries/source.too")
    if through_directory:
        (directory / "nested").mkdir()
        (layout.agent_home / "directory").symlink_to("entries/nested")
        layout.program.symlink_to("directory/../source.too")
    else:
        layout.program.symlink_to("intermediate")
    mounts = (SandboxMount(layout.agent_home, Path("/guest/agents/alice")),)
    validate_source_mounts(layout, mounts)


def test_external_catalog_override_ignores_unused_implicit_catalog_links(tmp_path):
    from toolang.plugin.catalogs.models_dev.path import ModelCatalogSelection

    layout = AgentLayout.resident(tmp_path / "root", "alice")
    layout.agent_home.mkdir(parents=True)
    implicit = tmp_path / "implicit.json"
    implicit.write_text("{}")
    (layout.agent_home / "catalog.json").symlink_to(implicit)
    explicit = tmp_path / "explicit.json"
    explicit.write_text("{}")
    mounts = (
        SandboxMount(layout.agent_home, Path("/guest/agents/alice")),
        SandboxMount(explicit, Path("/guest/.inputs/catalog.json"), read_only=True),
    )
    validate_source_mounts(
        layout, mounts, catalog=ModelCatalogSelection(explicit, "custom")
    )


def test_source_link_uses_its_authored_mount_when_also_granted_as_a_workspace(tmp_path):
    layout = AgentLayout.resident(tmp_path, "alice")
    runnables = layout.agent_home / "runnables"
    runnables.mkdir(parents=True)
    (layout.agent_home / "entry-source").write_text("flow work():\n  pass\n")
    (runnables / "extra.too").symlink_to("../entry-source")
    mounts = (
        SandboxMount(layout.agent_home, Path("/guest/agents/alice")),
        SandboxMount(runnables, Path("/guest/workspaces/alice/modules")),
    )
    validate_source_mounts(layout, mounts)


def test_workspace_mounts_snapshot_only_available_grants(tmp_path: Path) -> None:
    local_home = tmp_path / "agents/alice"
    local_home.mkdir(parents=True)
    repo = tmp_path / "repo"
    sdk = repo / "sdk"
    sdk.mkdir(parents=True)
    missing = tmp_path / "missing"
    configured_lab = tmp_path / "user-lab"
    configured_lab.mkdir()
    (local_home / "config.toml").write_text(
        f'[workspaces]\nrepo = "{repo}"\nsdk = "{sdk}"\n'
        f'lab = "{configured_lab}"\nmissing = "{missing}"\n'
    )
    hosted_workspaces = Path("/root/.toolang/workspaces/alice")
    mounts, mapping = prepare_workspace_mounts(
        local_home,
        hosted_workspaces,
        workspaces=prepare_agent_state(
            AgentLayout.resident(tmp_path, "alice")
        ).workspaces,
    )
    lab_root = tmp_path / "workspaces/alice/lab"
    hosted_lab = hosted_workspaces / "lab"
    assert lab_root.is_dir()
    assert mounts == (
        SandboxMount(lab_root, hosted_lab),
        SandboxMount(repo.resolve(), hosted_workspaces / "repo"),
        SandboxMount(sdk.resolve(), hosted_workspaces / "sdk"),
    )
    assert mapping == {
        "repo": (str(repo), str(hosted_workspaces / "repo")),
        "sdk": (str(sdk), str(hosted_workspaces / "sdk")),
        "lab": (str(lab_root.resolve()), str(hosted_lab)),
    }
    assert not any(mount.local_path == configured_lab for mount in mounts)


def test_workspace_mounts_include_configured_tmp_as_ordinary_grant(
    tmp_path: Path,
) -> None:
    local_home = tmp_path / "agents/alice"
    local_home.mkdir(parents=True)
    configured = tmp_path / "configured-tmp"
    configured.mkdir()
    (local_home / "config.toml").write_text(f'[workspaces]\ntmp = "{configured}"\n')
    hosted_workspaces = Path("/root/.toolang/workspaces/alice")

    mounts, mapping = prepare_workspace_mounts(
        local_home,
        hosted_workspaces,
        workspaces=prepare_agent_state(
            AgentLayout.resident(tmp_path, "alice")
        ).workspaces,
    )

    lab_root = tmp_path / "workspaces/alice/lab"
    assert mapping == {
        "lab": (str(lab_root.resolve()), str(hosted_workspaces / "lab")),
        "tmp": (str(configured), str(hosted_workspaces / "tmp")),
    }
    assert any(mount.local_path == configured for mount in mounts)


def test_workspace_mounts_reject_symlink_lab_directory(tmp_path: Path) -> None:
    local_home = tmp_path / "agents/alice"
    local_home.mkdir(parents=True)
    target = tmp_path / "outside"
    target.mkdir()
    lab = tmp_path / "workspaces/alice/lab"
    lab.parent.mkdir(parents=True)
    lab.symlink_to(target, target_is_directory=True)

    with pytest.raises(
        ValueError, match="implicit lab workspace must not be a symlink"
    ):
        prepare_workspace_mounts(
            local_home, Path("/root/.toolang/workspaces/alice"), workspaces={}
        )


def test_workspace_mounts_reject_file_at_lab_path(tmp_path: Path) -> None:
    local_home = tmp_path / "agents/alice"
    local_home.mkdir(parents=True)
    lab = tmp_path / "workspaces/alice/lab"
    lab.parent.mkdir(parents=True)
    lab.write_text("not a directory")

    with pytest.raises(FileExistsError):
        prepare_workspace_mounts(
            local_home, Path("/root/.toolang/workspaces/alice"), workspaces={}
        )


def test_nested_sources_keep_their_distinct_workspace_names(tmp_path: Path) -> None:
    local_home = tmp_path / "agents/alice"
    local_home.mkdir(parents=True)
    root = tmp_path / "repo"
    child = root / "child"
    child.mkdir(parents=True)
    (local_home / "config.toml").write_text(
        f'[workspaces]\naa = "{child}"\nzz = "{root}"\n'
    )
    mounts, mapping = prepare_workspace_mounts(
        local_home,
        Path("/guest/workspaces/alice"),
        workspaces=prepare_agent_state(
            AgentLayout.resident(tmp_path, "alice")
        ).workspaces,
    )
    assert [mount.local_path for mount in mounts] == [
        tmp_path / "workspaces/alice/lab",
        root.resolve(),
        child.resolve(),
    ]
    assert mapping["aa"][1] == "/guest/workspaces/alice/aa"
    assert mapping["zz"][1] == "/guest/workspaces/alice/zz"


def test_workspace_mounts_keep_captured_state_when_source_changes(tmp_path):
    layout = AgentLayout.resident(tmp_path, "alice")
    layout.home.mkdir(parents=True)
    old = tmp_path / "old"
    new = tmp_path / "new"
    old.mkdir()
    new.mkdir()
    layout.config.write_text(f'[workspaces]\nrepo = "{old}"\n')
    state = prepare_agent_state(layout)
    layout.config.write_text(f'[workspaces]\nrepo = "{new}"\n')
    _, mapping = prepare_workspace_mounts(
        layout.home, Path("/guest/workspaces/alice"), workspaces=state.workspaces
    )
    assert mapping["repo"][0] == str(old)


@pytest.mark.parametrize("scope", ["root", "place", "home"])
def test_custom_catalog_uses_fixed_read_only_target_even_when_covered(tmp_path, scope):
    directory = tmp_path / scope
    directory.mkdir()
    catalog = directory / "catalog.json"
    catalog.write_text("{}")
    additional, target = prepare_catalog_mount(catalog, Path("/guest/place"))
    assert target == Path("/guest/place/.inputs/catalog.json")
    assert additional == (SandboxMount(catalog, target, read_only=True),)


def test_external_catalog_preserves_its_filename_and_is_read_only(tmp_path):
    catalog = tmp_path / "custom-models.json"
    catalog.write_text("{}")
    mounts, target = prepare_catalog_mount(catalog, Path("/guest/place"))
    assert target == Path("/guest/place/.inputs/catalog.json")
    assert mounts == (SandboxMount(catalog, target, read_only=True),)


def test_workspace_keeps_writes_when_home_has_a_read_only_config_override(tmp_path):
    home = tmp_path / "agents/alice"
    home.mkdir(parents=True)
    guest = Path("/guest/agents/alice")
    (home / "config.toml").write_text('[workspaces]\nhome = "."\n')
    inputs = prepare_place_mounts(tmp_path, Path("/guest"))
    assert not any(mount.hosted_path == guest / "config.toml" for mount in inputs)
    guest_workspaces = Path("/guest/workspaces/alice")
    mounts, mapping = prepare_workspace_mounts(
        home,
        guest_workspaces,
        workspaces={"home": str(home)},
    )
    writable_home = guest_workspaces / "home"
    assert mapping["home"] == (str(home), str(writable_home))
    assert SandboxMount(home, writable_home) in mounts
    assert mapping["lab"][1] == str(guest_workspaces / "lab")


def test_root_inputs_do_not_expose_a_same_named_resident_home(tmp_path):
    root = tmp_path / "global"
    peer = root / "agents/alice"
    peer.mkdir(parents=True)
    secret = tmp_path / "private.too"
    secret.write_text("private\n")
    (peer / "agent.too").symlink_to(secret)
    mounts = prepare_place_mounts(
        root,
        Path("/guest/root"),
    )
    assert not any(mount.local_path == secret for mount in mounts)
    assert not any(
        mount.hosted_path.is_relative_to(Path("/guest/root/agents")) for mount in mounts
    )
