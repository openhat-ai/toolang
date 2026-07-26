from __future__ import annotations

from pathlib import Path

from toolang.up.mounts import prepare_root_mounts


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
