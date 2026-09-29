"""Agent-owned hosted filesystem assembly."""

from __future__ import annotations

from pathlib import Path

from toolang.base.types.sandbox import SandboxMount
from toolang.common.layout import (
    IMPLICIT_WORKSPACE_NAME,
    ensure_scratch_workspace,
)
from toolang.state.config import ConfiguredWorkspaces
from toolang.state.source import observe_home_source, observe_root_source

_ROOT_MOUNT_DIR_NAMES = ("psyches", "skills", "services", "prompts")


def prepare_root_mounts(
    local_root: Path,
    hosted_root: Path,
) -> tuple[SandboxMount, ...]:
    """Prepare Toolang root paths and return their explicit hosted mounts."""

    config_path = local_root / "config.toml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.touch(exist_ok=True)

    mounts = [
        SandboxMount(
            local_path=config_path,
            hosted_path=hosted_root / "config.toml",
        )
    ]
    catalog_path = local_root / "catalog.json"
    if catalog_path.is_file():
        mounts.append(
            SandboxMount(
                local_path=catalog_path.resolve(strict=True),
                hosted_path=hosted_root / "catalog.json",
                read_only=True,
            )
        )
    for directory_name in (".state", *_ROOT_MOUNT_DIR_NAMES):
        local_path = local_root / directory_name
        local_path.mkdir(parents=True, exist_ok=True)
        mounts.append(
            SandboxMount(
                local_path=local_path,
                hosted_path=hosted_root / directory_name,
            )
        )
    return tuple(mounts)


def prepare_linked_state_source_mounts(
    local_root: Path,
    agent_name: str,
    hosted_root: Path,
) -> tuple[SandboxMount, ...]:
    """Mount every symbolic-linked State source at its logical guest path."""

    hosted_home = hosted_root / "agents" / agent_name
    observations = (
        (observe_root_source(local_root), hosted_root),
        (observe_home_source(local_root, agent_name), hosted_home),
    )
    return tuple(
        SandboxMount(
            local_path=item.source.resolve(strict=True),
            hosted_path=hosted_base / item.path,
            read_only=True,
        )
        for observation, hosted_base in observations
        for item in observation.files
        if item.source.is_symlink()
    )


def prepare_workspace_mounts(
    local_home: Path, hosted_home: Path
) -> tuple[tuple[SandboxMount, ...], dict[str, tuple[str, str]]]:
    """Capture mounted grants at sandbox startup; never mount later State additions."""
    scratch_root = ensure_scratch_workspace(local_home)
    configured = ConfiguredWorkspaces(local_home / "config.toml").list()
    grants = {IMPLICIT_WORKSPACE_NAME: str(scratch_root)}
    grants.update(
        (name, source)
        for name, source in configured.items()
        if name != IMPLICIT_WORKSPACE_NAME
    )
    available = sorted(
        (
            (name, source, Path(source).resolve())
            for name, source in grants.items()
            if Path(source).is_dir()
        ),
        key=lambda item: (len(item[2].parts), item[0]),
    )
    mounts: list[SandboxMount] = []
    mapping: dict[str, tuple[str, str]] = {}
    # Preserve the nesting of State roots in the guest so OS-absolute paths
    # select the same most-specific workspace on both sides of the boundary.
    for name, source, root in available:
        ancestors = [
            (parent, mount.hosted_path)
            for mount in mounts
            if root.is_relative_to(parent := mount.local_path)
        ]
        if ancestors:
            parent, guest_parent = max(ancestors, key=lambda item: len(item[0].parts))
            guest = guest_parent / root.relative_to(parent)
        else:
            guest = hosted_home / ".workspaces" / name
        mounts.append(SandboxMount(local_path=root, hosted_path=guest))
        mapping[name] = (source, str(guest))
    return tuple(mounts), mapping
