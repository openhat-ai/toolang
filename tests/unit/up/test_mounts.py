from __future__ import annotations

from pathlib import Path

from toolang.common.layout import AgentLayout
from toolang.state.prepare import prepare_agent_state

import pytest

from toolang.base.types.sandbox import SandboxMount
from toolang.up.mounts import (
    prepare_catalog_mount,
    prepare_linked_state_source_mounts,
    prepare_place_mounts,
    prepare_workspace_mounts,
    prepare_source_mounts,
)


def test_prepare_place_mounts_owns_place_inputs(tmp_path: Path) -> None:
    local_place = tmp_path / "toolang"
    hosted_place = Path("/root/.toolang")

    mounts = prepare_place_mounts(local_place, hosted_place)

    assert {(item.local_path, item.hosted_path) for item in mounts} == {
        (local_place / "config.toml", hosted_place / "config.toml"),
        (local_place / ".state", hosted_place / ".state"),
        (local_place / "psyches", hosted_place / "psyches"),
        (local_place / "skills", hosted_place / "skills"),
        (local_place / "services", hosted_place / "services"),
        (local_place / "prompts", hosted_place / "prompts"),
    }
    assert (local_place / "config.toml").is_file()
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


def test_prepare_linked_state_source_mounts_covers_root_and_home_sources(
    tmp_path: Path,
) -> None:
    local_place = tmp_path / "toolang"
    local_home = local_place / "agents" / "alice"
    local_home.mkdir(parents=True)
    hosted_place = Path("/root/.toolang")
    hosted_home = hosted_place / "agents" / "alice"
    external = tmp_path / "external"
    external.mkdir()
    sources = {
        local_home / "agent.too": external / "agent.too",
        local_home / "config.toml": external / "config.toml",
        local_place / "prompts" / "review.md": external / "review.md",
        local_home / "runnables" / "research.too": external / "research.too",
        local_home / "skills" / "pdf" / "SKILL.md": external / "SKILL.md",
    }
    for logical, target in sources.items():
        logical.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("source\n", encoding="utf-8")
        logical.symlink_to(target)

    mounts = prepare_linked_state_source_mounts(
        local_place,
        "alice",
        hosted_place,
    )

    assert set(mounts) == {
        SandboxMount(
            target,
            (
                hosted_home / logical.relative_to(local_home)
                if logical.is_relative_to(local_home)
                else hosted_place / logical.relative_to(local_place)
            ),
            read_only=True,
        )
        for logical, target in sources.items()
    }


@pytest.mark.parametrize("linked", [False, True])
def test_place_catalog_is_mounted_read_only(tmp_path: Path, linked: bool) -> None:
    local_place = tmp_path / "toolang"
    local_place.mkdir()
    catalog = local_place / "catalog.json"
    source = tmp_path / "external.json" if linked else catalog
    source.write_text("{}")
    if linked:
        catalog.symlink_to(source)
    hosted_place = Path("/root/.toolang")

    assert SandboxMount(
        source.resolve(), hosted_place / "catalog.json", read_only=True
    ) in prepare_place_mounts(local_place, hosted_place)


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
def test_catalog_reuses_its_original_scope_and_filename(tmp_path, scope):
    directory = tmp_path / scope
    directory.mkdir()
    catalog = directory / "catalog.json"
    catalog.write_text("{}")
    guest = Path("/guest") / scope
    mounts = (SandboxMount(directory, guest),)
    additional, target = prepare_catalog_mount(
        catalog, Path("/guest/place"), mounts=mounts
    )
    assert additional == ()
    assert target == guest / "catalog.json"


def test_external_catalog_preserves_its_filename_and_is_read_only(tmp_path):
    catalog = tmp_path / "custom-models.json"
    catalog.write_text("{}")
    mounts, target = prepare_catalog_mount(catalog, Path("/guest/place"), mounts=())
    assert target.name == catalog.name
    assert target.is_relative_to(Path("/guest/place/.inputs/models"))
    assert mounts == (SandboxMount(catalog, target, read_only=True),)


def test_workspace_keeps_writes_when_home_has_a_read_only_config_override(tmp_path):
    home = tmp_path / "agents/alice"
    home.mkdir(parents=True)
    guest = Path("/guest/agents/alice")
    (home / "config.toml").write_text('[workspaces]\nhome = "."\n')
    inputs = prepare_source_mounts(tmp_path, "alice", Path("/guest"))
    assert any(
        mount.hosted_path == guest / "config.toml" and mount.read_only
        for mount in inputs
    )
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
    visitor_home = tmp_path / "visitor/agents/alice"
    mounts = prepare_source_mounts(
        root,
        "alice",
        Path("/guest/root"),
        capture_home=visitor_home,
        include_home=False,
    )
    assert not any(mount.local_path == secret for mount in mounts)
    assert not any(
        mount.hosted_path.is_relative_to(Path("/guest/root/agents")) for mount in mounts
    )
