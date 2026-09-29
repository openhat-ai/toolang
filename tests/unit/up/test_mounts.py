from __future__ import annotations

from pathlib import Path

import pytest

from toolang.base.types.sandbox import SandboxMount
from toolang.up.mounts import (
    prepare_linked_state_source_mounts,
    prepare_root_mounts,
    prepare_workspace_mounts,
)


def test_prepare_root_mounts_owns_toolang_layout(tmp_path: Path) -> None:
    local_root = tmp_path / "toolang"
    hosted_root = Path("/root/.toolang")

    mounts = prepare_root_mounts(local_root, hosted_root)

    assert {(item.local_path, item.hosted_path) for item in mounts} == {
        (local_root / "config.toml", hosted_root / "config.toml"),
        (local_root / ".state", hosted_root / ".state"),
        (local_root / "psyches", hosted_root / "psyches"),
        (local_root / "skills", hosted_root / "skills"),
        (local_root / "services", hosted_root / "services"),
        (local_root / "prompts", hosted_root / "prompts"),
    }
    assert (local_root / "config.toml").is_file()
    assert not (local_root / ".setup").exists()
    assert all(
        (local_root / name).is_dir()
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
    local_root = tmp_path / "toolang"
    local_home = local_root / "agents" / "alice"
    local_home.mkdir(parents=True)
    hosted_root = Path("/root/.toolang")
    hosted_home = hosted_root / "agents" / "alice"
    external = tmp_path / "external"
    external.mkdir()
    sources = {
        local_home / "agent.too": external / "agent.too",
        local_home / "config.toml": external / "config.toml",
        local_root / "prompts" / "review.md": external / "review.md",
        local_home / "flows" / "research.too": external / "research.too",
        local_home / "skills" / "pdf" / "SKILL.md": external / "SKILL.md",
    }
    for logical, target in sources.items():
        logical.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("source\n", encoding="utf-8")
        logical.symlink_to(target)

    mounts = prepare_linked_state_source_mounts(
        local_root,
        "alice",
        hosted_root,
    )

    assert set(mounts) == {
        SandboxMount(
            target,
            (
                hosted_home / logical.relative_to(local_home)
                if logical.is_relative_to(local_home)
                else hosted_root / logical.relative_to(local_root)
            ),
            read_only=True,
        )
        for logical, target in sources.items()
    }


@pytest.mark.parametrize("linked", [False, True])
def test_root_catalog_is_mounted_read_only(tmp_path: Path, linked: bool) -> None:
    local_root = tmp_path / "toolang"
    local_root.mkdir()
    catalog = local_root / "catalog.json"
    source = tmp_path / "external.json" if linked else catalog
    source.write_text("{}")
    if linked:
        catalog.symlink_to(source)
    hosted_root = Path("/root/.toolang")

    assert SandboxMount(
        source.resolve(), hosted_root / "catalog.json", read_only=True
    ) in prepare_root_mounts(local_root, hosted_root)


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
    hosted_home = Path("/root/.toolang/agents/alice")
    mounts, mapping = prepare_workspace_mounts(local_home, hosted_home)
    lab_root = local_home / "lab"
    hosted_lab = hosted_home / ".workspaces/lab"
    assert lab_root.is_dir()
    assert mounts == (
        SandboxMount(repo.resolve(), hosted_home / ".workspaces/repo"),
        SandboxMount(sdk.resolve(), hosted_home / ".workspaces/repo/sdk"),
        SandboxMount(lab_root.resolve(), hosted_lab),
    )
    assert mapping == {
        "repo": (str(repo), str(hosted_home / ".workspaces/repo")),
        "sdk": (str(sdk), str(hosted_home / ".workspaces/repo/sdk")),
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
    hosted_home = Path("/root/.toolang/agents/alice")

    mounts, mapping = prepare_workspace_mounts(local_home, hosted_home)

    lab_root = local_home / "lab"
    assert mapping == {
        "lab": (str(lab_root.resolve()), str(hosted_home / ".workspaces/lab")),
        "tmp": (str(configured), str(hosted_home / ".workspaces/tmp")),
    }
    assert any(mount.local_path == configured for mount in mounts)


def test_workspace_mounts_reject_symlink_lab_directory(tmp_path: Path) -> None:
    local_home = tmp_path / "agents/alice"
    local_home.mkdir(parents=True)
    target = tmp_path / "outside"
    target.mkdir()
    (local_home / "lab").symlink_to(target, target_is_directory=True)

    with pytest.raises(
        ValueError, match="implicit lab workspace must not be a symlink"
    ):
        prepare_workspace_mounts(local_home, Path("/root/.toolang/agents/alice"))


def test_workspace_mounts_reject_file_at_lab_path(tmp_path: Path) -> None:
    local_home = tmp_path / "agents/alice"
    local_home.mkdir(parents=True)
    (local_home / "lab").write_text("not a directory")

    with pytest.raises(FileExistsError):
        prepare_workspace_mounts(local_home, Path("/root/.toolang/agents/alice"))


def test_nested_mount_order_is_by_root_depth_not_workspace_name(tmp_path: Path) -> None:
    local_home = tmp_path / "agents/alice"
    local_home.mkdir(parents=True)
    root = tmp_path / "repo"
    child = root / "child"
    child.mkdir(parents=True)
    (local_home / "config.toml").write_text(
        f'[workspaces]\naa = "{child}"\nzz = "{root}"\n'
    )
    mounts, mapping = prepare_workspace_mounts(local_home, Path("/guest/alice"))
    assert [mount.local_path for mount in mounts] == [
        root.resolve(),
        child.resolve(),
        (local_home / "lab").resolve(),
    ]
    assert mapping["aa"][1] == "/guest/alice/.workspaces/zz/child"
    assert mounts[1].hosted_path.is_relative_to(mounts[0].hosted_path)
