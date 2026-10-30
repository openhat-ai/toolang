from pathlib import Path
import errno
import json
import subprocess

import pytest

from toolang.common.config_sources import config_sources
from toolang.up.source import materialize_roaming_program


def _inputs(tmp_path: Path) -> tuple[Path, Path, Path]:
    project = tmp_path / "project"
    directory = project / "team" / "demo"
    directory.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(project)], check=True)
    (project / "toolang.toml").write_text('[models]\ndefault = "outer"\n')
    (directory.parent / "toolang.toml").write_text('[models]\ndefault = "middle"\n')
    config = directory / "toolang.toml"
    config.write_text('[models]\ndefault = "nearest"\n')
    (directory / "toolang.catalog.json").write_text("{}")
    program = directory / "a.too"
    program.write_text("flow before():\n  pass\n")
    return program, config, directory / "toolang.catalog.json"


def test_roaming_fetch_links_nearest_sources_without_projection(tmp_path: Path):
    program, config, catalog = _inputs(tmp_path)
    layout = materialize_roaming_program(program, toolang_root=tmp_path / "root")
    assert layout.program.samefile(program)
    assert layout.root_config == program.parent / ".toolang/config/config.toml"
    assert layout.root_config.samefile(config)
    assert (layout.agent_root / "config/catalog.json").samefile(catalog)
    assert not layout.program.is_symlink()
    assert layout.root_config.read_bytes() == config.read_bytes()
    receipt = json.loads((layout.agent_home / "source.json").read_bytes())
    assert receipt["source_path"] == str(program)
    assert json.loads(layout.root_source_receipt.read_bytes())["origins"][
        "root"
    ] == str(config)
    assert [
        item.config["models"]
        for item in config_sources(layout)
        if "models" in item.config
    ] == [{"default": "nearest"}]


@pytest.mark.parametrize("index", [0, 1, 2])
def test_roaming_relinks_replaced_source_only_on_startup(tmp_path: Path, index: int):
    sources = _inputs(tmp_path)
    layout = materialize_roaming_program(sources[0], toolang_root=tmp_path / "root")
    targets = (
        layout.program,
        layout.root_config,
        layout.agent_root / "config/catalog.json",
    )
    source = sources[index]
    target = targets[index]
    before = target.read_bytes()
    replacement = source.with_suffix(".replacement")
    replacement.write_bytes(before + b"\n")
    replacement.replace(source)
    assert target.read_bytes() == before
    materialize_roaming_program(sources[0], layout=layout)
    assert target.samefile(source)
    assert target.read_bytes() == before + b"\n"


def test_empty_nearest_toml_stops_ancestor_inheritance(tmp_path: Path):
    program, config, _ = _inputs(tmp_path)
    config.write_text("")
    layout = materialize_roaming_program(program, toolang_root=tmp_path / "root")
    assert layout.root_config.samefile(config)
    assert all("models" not in item.config for item in config_sources(layout))


def test_invalid_nearest_toml_is_not_replaced_by_valid_ancestor(tmp_path: Path):
    program, config, _ = _inputs(tmp_path)
    config.write_text("[invalid")
    with pytest.raises(ValueError, match="configuration"):
        materialize_roaming_program(program, toolang_root=tmp_path / "root")


def test_relative_workspace_uses_original_toml_directory(tmp_path: Path):
    from toolang.state.prepare import prepare_agent_state

    program, config, _ = _inputs(tmp_path)
    config.write_text('[workspaces]\nrepo = "."\n')
    layout = materialize_roaming_program(program, toolang_root=tmp_path / "root")
    state = prepare_agent_state(layout)
    assert state.workspaces["repo"] == str(program.parent)


def test_config_origin_is_shared_between_different_programs(tmp_path: Path):
    program, config, _ = _inputs(tmp_path)
    first = materialize_roaming_program(program, toolang_root=tmp_path / "root")
    second_program = program.with_name("b.too")
    second_program.write_bytes(program.read_bytes())
    config.unlink()
    materialize_roaming_program(second_program, toolang_root=tmp_path / "other-root")
    root = next(item for item in config_sources(first) if item.scope == "root")
    assert root.path == program.parent.parent / "toolang.toml"
    assert root.config["models"] == {"default": "middle"}


def test_roaming_hardlinks_preserve_source_modes(tmp_path: Path):
    sources = _inputs(tmp_path)
    for source in sources:
        source.chmod(0o640)
    materialize_roaming_program(sources[0], toolang_root=tmp_path / "root")
    assert all(source.stat().st_mode & 0o777 == 0o640 for source in sources)


def test_failed_relink_preserves_the_previous_placement_file(tmp_path, monkeypatch):
    from toolang.up import source as acquisition

    sources = _inputs(tmp_path)
    layout = materialize_roaming_program(sources[0], toolang_root=tmp_path / "root")
    before = layout.program.read_bytes()
    replacement = sources[0].with_suffix(".replacement")
    replacement.write_bytes(before + b"\n")
    replacement.replace(sources[0])

    def cross_device(*args, **kwargs):
        raise OSError(errno.EXDEV, "cross-device link")

    monkeypatch.setattr(acquisition.os, "link", cross_device)
    with pytest.raises(OSError, match="cross-device"):
        materialize_roaming_program(sources[0], layout=layout)
    assert layout.program.read_bytes() == before
    assert not list(layout.agent_home.glob(".agent.too.*"))


def test_roaming_host_observes_retargeted_workspace_symlink(tmp_path):
    from toolang.state.prepare import prepare_agent_state

    program, config, _ = _inputs(tmp_path)
    before, after = program.parent / "before", program.parent / "after"
    before.mkdir()
    after.mkdir()
    link = program.parent / "repo"
    link.symlink_to(before, target_is_directory=True)
    config.write_text('[workspaces]\nrepo = "./repo"\n')
    layout = materialize_roaming_program(program, toolang_root=tmp_path / "root")
    first = prepare_agent_state(layout)
    assert first.workspaces["repo"] == str(before)
    link.unlink()
    link.symlink_to(after, target_is_directory=True)
    second = prepare_agent_state(layout)
    assert second.workspaces["repo"] == str(after)
    assert second.revision != first.revision
