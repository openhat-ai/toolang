from __future__ import annotations

from pathlib import Path

from toolang.base.types.sandbox import SandboxMount
from toolang.up.mounts import (
    prepare_linked_state_source_mounts,
    prepare_root_mounts,
)


def test_prepare_root_mounts_owns_toolang_layout(tmp_path: Path) -> None:
    local_root = tmp_path / "toolang"
    hosted_root = Path("/root/.toolang")

    mounts = prepare_root_mounts(local_root, hosted_root)

    assert {(item.local_path, item.hosted_path) for item in mounts} == {
        (local_root / "config.toml", hosted_root / "config.toml"),
        (local_root / ".setup", hosted_root / ".setup"),
        (local_root / ".state", hosted_root / ".state"),
        (local_root / "psyches", hosted_root / "psyches"),
        (local_root / "skills", hosted_root / "skills"),
        (local_root / "services", hosted_root / "services"),
        (local_root / "prompts", hosted_root / "prompts"),
    }
    assert (local_root / "config.toml").is_file()
    assert all(
        (local_root / name).is_dir()
        for name in (
            ".setup",
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
