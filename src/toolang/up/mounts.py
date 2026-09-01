"""Agent-owned hosted filesystem assembly."""

from __future__ import annotations

from pathlib import Path

from toolang.base.types.sandbox import SandboxMount
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
    for directory_name in (".setup", ".state", *_ROOT_MOUNT_DIR_NAMES):
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
