"""Portable project configuration uses authored origins and Git boundaries."""

import subprocess

import pytest

from toolang.common.config_sources import (
    config_sources,
    script_directories,
)
from toolang.common.layout import AgentLayout
from toolang.plugin.catalogs.models_dev.path import resolve_model_catalog_path
from toolang.setup.config import (
    resolve_run_defaults,
    resolve_compact_config,
    resolve_setup_allow,
)
from toolang.plugin.config import merge_plugin_configs
from toolang.up.sandbox import resolve_selection
from toolang.state.prepare import prepare_agent_state
from toolang.up.source import materialize_roaming_program
from toolang.up.mounts import prepare_place_mounts


@pytest.mark.parametrize("scope", ["root", "home"])
def test_declared_local_catalog_keeps_directory_ownership_in_the_guest(tmp_path, scope):
    from dataclasses import replace

    from toolang.common.config_runtime import CONFIG_PATCH_ENV, RuntimeConfig
    from toolang.plugin.catalogs.models_dev.path import resolve_model_catalog_selection
    from toolang.up.mounts import prepare_guest_environment

    host = AgentLayout.resident(tmp_path / "host", "alice")
    guest = AgentLayout.resident(tmp_path / "guest", "alice")
    config = host.root_config if scope == "root" else host.config
    guest_config = guest.root_config if scope == "root" else guest.config
    for path in (config, guest_config):
        (path.parent / "nested").mkdir(parents=True)
        for name in ("one", "two"):
            (path.parent / f"nested/{name}.json").write_text("{}")
    host.agent_home.mkdir(parents=True, exist_ok=True)
    for name in ("one", "two"):
        contents = f'[plugin.model_catalog.models_dev]\npath = "nested/{name}.json"\n'
        config.write_text(contents)
        guest_config.write_text(contents)
        selected = resolve_model_catalog_selection(host)
        assert selected.scope == scope
        if name == "one":
            environment = prepare_guest_environment(host, mapping={}, catalog=selected)
            guest = replace(
                guest,
                runtime_config=RuntimeConfig.parse(environment[CONFIG_PATCH_ENV]),
            )
        observed = resolve_model_catalog_selection(guest)
        assert observed.scope == scope
        assert observed.path == guest_config.parent / f"nested/{name}.json"


@pytest.mark.parametrize("declared", [False, True])
def test_removed_roaming_catalog_obeys_local_selection_without_refetch(
    tmp_path, declared
):
    from toolang.plugin.catalogs.models_dev.path import PACKAGED_MODEL_CATALOG

    source = tmp_path / "aid.too"
    source.write_text("flow run():\n  pass\n")
    catalog = tmp_path / "toolang.catalog.json"
    catalog.write_text('{"providers": [], "models": []}')
    (tmp_path / "toolang.toml").write_text(
        '[plugin.model_catalog.models_dev]\npath = "toolang.catalog.json"\n'
        if declared
        else ""
    )
    layout = materialize_roaming_program(source, toolang_root=tmp_path / "root")
    assert resolve_model_catalog_path(layout) == layout.root_catalog
    layout.root_catalog.unlink()
    assert catalog.is_file()
    if declared:
        with pytest.raises(FileNotFoundError, match="catalog"):
            resolve_model_catalog_path(layout)
    else:
        assert resolve_model_catalog_path(layout) == PACKAGED_MODEL_CATALOG
    materialize_roaming_program(source, layout=layout)
    assert resolve_model_catalog_path(layout).samefile(catalog)


@pytest.mark.parametrize("placement", ["resident", "roaming", "visiting"])
def test_shared_workspaces_are_ignored_before_path_resolution(tmp_path, placement):
    shared = '[workspaces]\nignored = "~toolang-nonexistent-user/repo"\n'
    if placement == "roaming":
        git_init(tmp_path)
        source = tmp_path / "module" / "aid.too"
        source.parent.mkdir()
        source.write_text("flow run():\n  pass\n")
        (tmp_path / "toolang.toml").write_text(shared)
        (source.parent / "toolang.toml").write_text('[workspaces]\nrepo = "."\n')
        layout = materialize_roaming_program(source, toolang_root=tmp_path)
        expected = str(source.parent)
    else:
        layout = AgentLayout(tmp_path, "helper", placement)
        layout.agent_home.mkdir(parents=True)
        layout.program.write_text("flow run():\n  pass\n")
        layout.place_config.parent.mkdir(parents=True, exist_ok=True)
        layout.place_config.write_text(shared)
        layout.config.write_text('[workspaces]\nrepo = "."\n')
        expected = str(layout.agent_home)

    sources = config_sources(layout)
    assert "workspaces" not in sources[0].config
    assert prepare_agent_state(layout).workspaces == {"repo": expected}
    # Guest snapshots must obey the same ownership rule as host loading.
    mounts = prepare_place_mounts(layout.agent_root, tmp_path / "guest")
    root_mount = next(m for m in mounts if m.hosted_path == tmp_path / "guest/config")
    assert (
        root_mount.local_path / "config.toml"
    ).read_bytes() == layout.place_config.read_bytes()


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
        '[model]\nref = "openai/gpt-4o"\n[workspaces]\nouter = "/"\n'
    )
    (project / "toolang.toml").write_text('[model]\neffort = "high"\n')
    (source_dir / "toolang.toml").write_text('[workspaces]\nrepo = ".."\n')
    monkeypatch.setenv("GIT_WORK_TREE", "/wrong")
    layout = materialize_roaming_program(source, toolang_root=tmp_path)
    sources = config_sources(layout)
    assert [item.path.parent for item in sources if item.scope == "root"] == [
        source_dir
    ]
    assert all("workspaces" not in item.config for item in sources)
    assert "workspaces" not in sources[0].config
    # Use the existing field-specific resolver on every authored layer.
    defaults = resolve_run_defaults([item.config for item in sources])
    assert defaults.model is None
    assert prepare_agent_state(layout).workspaces == {"repo": str(project)}
    assert layout == AgentLayout.roaming(
        source, place_code=layout.place_code, root=tmp_path
    )


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
    layout = materialize_roaming_program(source, toolang_root=tmp_path)
    assert resolve_model_catalog_path(layout).samefile(nearest)
    assert resolve_model_catalog_path(layout) == layout.root_catalog
    assert layout.root_catalog.samefile(nearest)
    nearest.unlink()
    materialize_roaming_program(source, toolang_root=tmp_path)
    assert resolve_model_catalog_path(layout).samefile(outer)


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
    layout = materialize_roaming_program(source, toolang_root=tmp_path)
    assert prepare_agent_state(layout).workspaces == {"repo": str(tmp_path)}
    config.unlink()
    materialize_roaming_program(source, toolang_root=tmp_path)
    assert prepare_agent_state(layout).workspaces == {}
    layout.place_config.parent.mkdir(parents=True, exist_ok=True)
    layout.place_config.write_text("# authored conflict\n")
    with pytest.raises(ValueError, match="conflicts"):
        materialize_roaming_program(source, toolang_root=tmp_path)
    assert layout.place_config.read_text() == "# authored conflict\n"


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
        materialize_roaming_program(source, toolang_root=tmp_path)


def test_real_script_and_discovered_config_symlinks_keep_distinct_origins(tmp_path):
    git_init(tmp_path)
    source_dir = tmp_path / "scripts"
    source_dir.mkdir()
    source = source_dir / "aid.too"
    source.write_text("flow run():\n  pass\n")
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "settings.toml").write_text('[workspaces]\nrepo = ".."\n')
    (source_dir / "toolang.toml").symlink_to(shared / "settings.toml")
    alias = tmp_path / "alias.too"
    alias.symlink_to(source)
    layout = materialize_roaming_program(alias, toolang_root=tmp_path)
    assert layout == AgentLayout.roaming(
        source, place_code=layout.place_code, root=tmp_path
    )
    assert prepare_agent_state(layout).workspaces == {"repo": str(tmp_path)}


@pytest.mark.parametrize("kind", ["worktree", "submodule"])
def test_git_boundary_does_not_inherit_parent(tmp_path, kind):
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
    parent = tmp_path / "parent"
    git_init(parent)
    (parent / "toolang.toml").write_text("[broken")
    worktree = parent / "linked"
    if kind == "worktree":
        command = [
            "git",
            "-C",
            str(repo),
            "worktree",
            "add",
            "-q",
            "--detach",
            str(worktree),
        ]
    else:
        command = [
            "git",
            "-C",
            str(parent),
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "add",
            "-q",
            str(repo),
            str(worktree),
        ]
    subprocess.run(command, check=True)
    inner = worktree / "module"
    inner.mkdir()
    source = inner / "aid.too"
    source.touch()
    assert script_directories(source) == (inner, worktree)
    assert all(
        not item.config
        for item in config_sources(
            materialize_roaming_program(source, toolang_root=tmp_path)
        )
    )


def test_three_project_candidates_use_only_the_nearest_file(tmp_path):
    git_init(tmp_path)
    middle = tmp_path / "project"
    inner = middle / "module"
    inner.mkdir(parents=True)
    source = inner / "aid.too"
    source.write_text("flow run():\n  pass\n")
    (tmp_path / "toolang.toml").write_text(
        '[model]\nref = "test/base"\n'
        '[compact.model]\nref = "test/old"\neffort = "high"\n'
        '[allow]\ntools = ["fs/*"]\n'
        '[sandbox]\ndriver = "docker"\ntarget = "old-image"\n'
        "[plugin.toolset.fs]\nvalues = [1, 2]\n"
        "[plugin.toolset.fs.options]\nouter = true\n"
    )
    (middle / "toolang.toml").write_text(
        '[model]\neffort = "high"\n'
        '[sandbox]\ndriver = "host"\n'
        "[plugin.toolset.fs.options]\ninner = true\n"
    )
    (inner / "toolang.toml").write_text(
        '[compact.model]\nref = "test/new"\n'
        '[allow]\ntools = ["shell/*"]\n'
        "[plugin.toolset.fs]\nvalues = [3]\n"
    )
    layout = materialize_roaming_program(source, toolang_root=tmp_path)
    configs = [item.config for item in config_sources(layout)]
    defaults = resolve_run_defaults(configs)
    assert defaults.model is None
    compact = resolve_compact_config(configs)
    assert compact.model[-1].identity == "test/new" and compact.model[-1].effort is None
    assert resolve_setup_allow(configs).tools == ("shell/*",)
    assert resolve_selection(layout) == "host"
    assert merge_plugin_configs(configs, family="toolset")["fs"] == {
        "values": [3],
    }


def test_unowned_symlink_is_not_replaced(tmp_path):
    source = tmp_path / "aid.too"
    source.write_text("flow run():\n  pass\n")
    layout = materialize_roaming_program(source, toolang_root=tmp_path)
    unrelated = tmp_path / "unrelated.toml"
    unrelated.write_text("# keep me\n")
    layout.place_config.unlink(missing_ok=True)
    layout.place_config.symlink_to(unrelated)
    with pytest.raises(ValueError, match="conflicts"):
        materialize_roaming_program(source, toolang_root=tmp_path)
    assert layout.place_config.is_symlink()
    assert unrelated.read_text() == "# keep me\n"


def test_explicit_catalog_can_override_a_missing_authored_catalog(tmp_path):
    source = tmp_path / "aid.too"
    source.write_text("flow run():\n  pass\n")
    (tmp_path / "toolang.toml").write_text(
        '[plugin.model_catalog.models_dev]\npath = "missing.json"\n'
    )
    layout = materialize_roaming_program(source, toolang_root=tmp_path)
    explicit = tmp_path / "explicit.json"
    explicit.write_text("{}")
    assert resolve_model_catalog_path(layout, explicit=explicit) == explicit
    with pytest.raises(FileNotFoundError, match="catalog"):
        resolve_model_catalog_path(layout)


def test_empty_catalog_path_does_not_become_the_config_directory(tmp_path):
    source = tmp_path / "aid.too"
    source.write_text("flow run():\n  pass\n")
    (tmp_path / "toolang.toml").write_text(
        '[plugin.model_catalog.models_dev]\npath = ""\n'
    )
    with pytest.raises(ValueError, match="model catalog path must be nonempty"):
        materialize_roaming_program(source, toolang_root=tmp_path)


def test_copied_home_does_not_claim_another_source_identity(tmp_path):
    import shutil

    original = tmp_path / "original"
    original.mkdir()
    source = original / "aid.too"
    source.write_text("flow run():\n  pass\n")
    (original / "toolang.catalog.json").write_text("{}")
    materialize_roaming_program(source, toolang_root=tmp_path)
    copied = tmp_path / "copied"
    shutil.copytree(original, copied, symlinks=True)
    (copied / "toolang.catalog.json").unlink()
    with pytest.raises(FileExistsError, match="already belongs"):
        materialize_roaming_program(copied / "aid.too", toolang_root=tmp_path)
    assert (original / "toolang.catalog.json").is_file()


def test_only_companion_directories_contribute_layers(tmp_path):
    git_init(tmp_path)
    middle = tmp_path / "empty" / "catalog-only"
    srcdir = middle / "empty" / "source"
    srcdir.mkdir(parents=True)
    source = srcdir / "aid.too"
    source.write_text("flow run():\n  pass\n")
    (tmp_path / "toolang.toml").write_text("# Shared settings\n")
    catalog = middle / "toolang.catalog.json"
    catalog.write_text("{}")

    layout = materialize_roaming_program(source, toolang_root=tmp_path)

    assert [
        item.path.parent for item in config_sources(layout) if item.scope == "root"
    ] == [tmp_path]
    assert resolve_model_catalog_path(layout).samefile(catalog)
    assert prepare_agent_state(layout).workspaces == {}


def test_roaming_dangling_home_catalog_does_not_fall_back_to_project(tmp_path):
    source = tmp_path / "aid.too"
    source.write_text("flow run():\n  pass\n")
    (tmp_path / "toolang.catalog.json").write_text("{}")
    layout = materialize_roaming_program(source, toolang_root=tmp_path / "global")
    (layout.agent_home / "catalog.json").symlink_to(tmp_path / "missing.json")
    with pytest.raises(ValueError, match="model catalog is not a file"):
        resolve_model_catalog_path(layout)
