"""Agent-owned sandbox filesystem assembly."""

from __future__ import annotations

from pathlib import Path

from toolang.base.types.sandbox import SandboxMount

_ROOT_MOUNT_DIR_NAMES = ("psyches", "skills", "services", "prompts")


def prepare_root_mounts(
    local_root: Path,
    sandbox_root: Path,
) -> tuple[SandboxMount, ...]:
    """Prepare Toolang root paths and return their explicit sandbox mounts."""

    config_path = local_root / "config.toml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.touch(exist_ok=True)

    mounts = [
        SandboxMount(
            local_path=config_path,
            sandbox_path=sandbox_root / "config.toml",
        )
    ]
    for directory_name in (".caps", *_ROOT_MOUNT_DIR_NAMES):
        local_path = local_root / directory_name
        local_path.mkdir(parents=True, exist_ok=True)
        mounts.append(
            SandboxMount(
                local_path=local_path,
                sandbox_path=sandbox_root / directory_name,
            )
        )
    return tuple(mounts)
