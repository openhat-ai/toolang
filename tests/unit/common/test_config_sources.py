"""Portable project configuration uses authored origins and Git boundaries."""

import subprocess

import pytest

from toolang.common.config_sources import (
    config_sources,
    script_directories,
    source_catalog,
)
from toolang.plugin.catalogs.models_dev.path import resolve_model_catalog_path
from toolang.setup.config import resolve_run_defaults
from toolang.state.prepare import prepare_agent_state
from toolang.up.process import materialize_roaming_program


def git_init(path):
    subprocess.run(["git", "init", "-q", str(path)], check=True)


def test_bounded_layers_keep_path_origins_and_local_permissions(tmp_path, monkeypatch):
    git_init(tmp_path)
    project = tmp_path / "project"
    source_dir = project / "module"
    source_dir.mkdir(parents=True)
    source = source_dir / "file.too"
    source.write_text("flow run():\n  pass\n")
    (tmp_path / "toolang.toml").write_text(
        '[default]\nmodel = "openai/gpt-4o"\n[workspaces]\nouter = "/"\n'
    )
    (project / "toolang.toml").write_text('[default]\nmodel = "effort=high"\n')
    (source_dir / "toolang.toml").write_text('[workspaces]\nrepo = ".."\n')
    monkeypatch.setenv("GIT_WORK_TREE", "/wrong")
    layout = materialize_roaming_program(source)
    sources = config_sources(layout)
    assert [item.path.parent for item in sources] == [tmp_path, project, source_dir]
    assert sources[-1].config["workspaces"] == {"repo": str(project)}
    assert "workspaces" not in sources[0].config
    # Use the existing field-specific resolver on every authored layer.
    defaults = resolve_run_defaults([item.config for item in sources])
    assert defaults.model is not None and defaults.model.ref == "openai/gpt-4o"
    assert prepare_agent_state(layout).workspaces == {"repo": str(project)}
    assert layout.root == source_dir / ".toolang"


def test_nearest_companion_beats_outer_catalog_path(tmp_path):
    git_init(tmp_path)
    inner = tmp_path / "inner"
    inner.mkdir()
    source = inner / "file.too"
    source.write_text("flow run():\n  pass\n")
    outer = tmp_path / "outer.json"
    outer.write_text("{}")
    nearest = inner / "toolang.catalog.json"
    nearest.write_text("{}")
    (tmp_path / "toolang.toml").write_text(
        '[plugin.model_catalog.models_dev]\npath = "outer.json"\n'
    )
    layout = materialize_roaming_program(source)
    assert resolve_model_catalog_path(layout) == nearest
    assert source_catalog(config_sources(layout), roaming=True) == nearest
    assert (layout.home / "catalog.json").resolve() == nearest
    nearest.unlink()
    materialize_roaming_program(source)
    assert resolve_model_catalog_path(layout) == outer


def test_non_git_and_nested_git_do_not_inherit_outer_config(tmp_path):
    inner = tmp_path / "inner"
    inner.mkdir()
    source = inner / "file.too"
    source.touch()
    assert script_directories(source) == (inner,)
    git_init(tmp_path)
    git_init(inner)
    assert script_directories(source) == (inner,)


def test_invalid_git_marker_is_not_skipped(tmp_path):
    (tmp_path / ".git").write_text("broken")
    source = tmp_path / "file.too"
    source.touch()
    with pytest.raises(ValueError, match="Git worktree"):
        script_directories(source)


def test_removed_source_config_clears_derived_settings(tmp_path):
    source = tmp_path / "file.too"
    source.write_text("flow run():\n  pass\n")
    config = tmp_path / "toolang.toml"
    config.write_text('[workspaces]\nrepo = "."\n')
    layout = materialize_roaming_program(source)
    assert prepare_agent_state(layout).workspaces == {"repo": str(tmp_path)}
    config.unlink()
    materialize_roaming_program(source)
    assert prepare_agent_state(layout).workspaces == {}
    layout.config.write_text("# authored conflict\n")
    with pytest.raises(ValueError, match="conflicts"):
        materialize_roaming_program(source)
    assert layout.config.read_text() == "# authored conflict\n"


@pytest.mark.parametrize("kind", ["directory", "dangling", "invalid"])
def test_bad_config_candidates_fail(tmp_path, kind):
    source = tmp_path / "file.too"
    source.touch()
    config = tmp_path / "toolang.toml"
    if kind == "directory":
        config.mkdir()
    elif kind == "dangling":
        config.symlink_to(tmp_path / "missing")
    else:
        config.write_text("[broken")
    with pytest.raises(ValueError, match="toolang.toml"):
        materialize_roaming_program(source)


def test_real_script_and_discovered_config_symlinks_keep_distinct_origins(tmp_path):
    git_init(tmp_path)
    source_dir = tmp_path / "scripts"
    source_dir.mkdir()
    source = source_dir / "aide.too"
    source.write_text("flow run():\n  pass\n")
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "settings.toml").write_text('[workspaces]\nrepo = ".."\n')
    (source_dir / "toolang.toml").symlink_to(shared / "settings.toml")
    alias = tmp_path / "alias.too"
    alias.symlink_to(source)
    layout = materialize_roaming_program(alias)
    assert layout.root == source_dir / ".toolang"
    assert prepare_agent_state(layout).workspaces == {"repo": str(tmp_path)}


def test_git_worktree_boundary_does_not_inherit_parent(tmp_path):
    repo = tmp_path / "repo"
    git_init(repo)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.test",
            "commit",
            "--allow-empty",
            "-qm",
            "initial",
        ],
        check=True,
    )
    worktree = tmp_path / "linked"
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "-q", "--detach", str(worktree)],
        check=True,
    )
    inner = worktree / "module"
    inner.mkdir()
    source = inner / "aide.too"
    source.touch()
    assert script_directories(source) == (inner, worktree)


def test_unowned_symlink_is_not_replaced(tmp_path):
    source = tmp_path / "aide.too"
    source.write_text("flow run():\n  pass\n")
    layout = materialize_roaming_program(source)
    unrelated = tmp_path / "unrelated.toml"
    unrelated.write_text("# keep me\n")
    layout.config.unlink()
    layout.config.symlink_to(unrelated)
    with pytest.raises(ValueError, match="symlink conflicts"):
        materialize_roaming_program(source)
    assert layout.config.is_symlink()
    assert unrelated.read_text() == "# keep me\n"


def test_explicit_catalog_can_override_a_missing_authored_catalog(tmp_path):
    source = tmp_path / "aide.too"
    source.write_text("flow run():\n  pass\n")
    (tmp_path / "toolang.toml").write_text(
        '[plugin.model_catalog.models_dev]\npath = "missing.json"\n'
    )
    layout = materialize_roaming_program(source)
    explicit = tmp_path / "explicit.json"
    explicit.write_text("{}")
    assert resolve_model_catalog_path(layout, explicit=explicit) == explicit
    with pytest.raises(ValueError, match="not a file"):
        resolve_model_catalog_path(layout)
