from __future__ import annotations

from pathlib import Path

from toolang.agent.sandbox import prepare_root_mounts


def test_prepare_root_mounts_owns_toolang_layout(tmp_path: Path) -> None:
    local_root = tmp_path / "toolang"
    sandbox_root = Path("/root/.toolang")

    mounts = prepare_root_mounts(local_root, sandbox_root)

    assert {(item.local_path, item.sandbox_path) for item in mounts} == {
        (local_root / "config.toml", sandbox_root / "config.toml"),
        (local_root / ".caps", sandbox_root / ".caps"),
        (local_root / "psyches", sandbox_root / "psyches"),
        (local_root / "skills", sandbox_root / "skills"),
        (local_root / "services", sandbox_root / "services"),
        (local_root / "prompts", sandbox_root / "prompts"),
    }
    assert (local_root / "config.toml").is_file()
    assert all(
        (local_root / name).is_dir()
        for name in (".caps", "psyches", "skills", "services", "prompts")
    )
