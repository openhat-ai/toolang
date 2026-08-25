"""Agent-owned hosted filesystem assembly."""

from __future__ import annotations

from pathlib import Path

from toolang.base.types.sandbox import SandboxMount

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
