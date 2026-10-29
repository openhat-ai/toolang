"""Root workspace policy follows stable agent identities and State snapshots."""

import asyncio
from dataclasses import replace

import pytest
import tomlkit

from toolang.common.layout import AgentLayout
from toolang.state.prepare import load_agent_state, prepare_agent_state
from toolang.state.watcher import StateWatcher


@pytest.mark.parametrize("placement", ["resident", "visiting", "roaming"])
def test_root_workspace_overrides_are_scoped_captured_and_reloadable(
    tmp_path, placement
):
    root = tmp_path / "root"
    root.mkdir()
    source = tmp_path / "project/check.too"
    source.parent.mkdir()
    source.write_text("flow:\n  pass\n")
    ref = "gh:example/check"
    layout = AgentLayout(
        root if placement == "resident" else tmp_path / "place",
        "check",
        placement,
        root=root,
        source_path=source if placement == "roaming" else None,
        source_ref=ref if placement == "visiting" else None,
    )
    layout.home.mkdir(parents=True)
    layout.agent_home.mkdir(parents=True, exist_ok=True)
    layout.program.write_text("flow:\n  pass\n")
    layout.config.write_text('[workspaces]\nrepo = "./old"\nnotes = "./notes"\n')
    key = {"resident": "check", "visiting": ref, "roaming": str(source)}[placement]
    config = layout.place_config
    config.write_text(tomlkit.dumps({"workspaces": {key: {"repo": "../external"}}}))

    before = prepare_agent_state(layout)
    assert dict(before.workspaces) == {
        "repo": str(config.parent.parent / "external"),
        "notes": str(layout.agent_home / "notes"),
    }
    assert load_agent_state(layout, before.revision).workspaces == before.workspaces

    # Other agents' grants and Setup fields do not change this home's policy snapshot.
    config.write_text(
        tomlkit.dumps(
            {
                "model": {"ref": "test/other"},
                "workspaces": {
                    key: {"repo": "../external"},
                    "peer": {"repo": "../peer"},
                },
            }
        )
    )
    assert prepare_agent_state(layout).home_revision == before.home_revision

    config.write_text(tomlkit.dumps({"workspaces": {key: {"repo": "../replacement"}}}))
    watcher = StateWatcher(layout, initial_state=before)
    after = asyncio.run(watcher.refresh())
    assert after.revision != before.revision
    assert after.workspaces["repo"] == str(config.parent.parent / "replacement")
    assert before.workspaces["repo"] == str(config.parent.parent / "external")
    assert load_agent_state(layout, before.revision).workspaces == before.workspaces

    config.write_text(tomlkit.dumps({"workspaces": {"another": {"repo": "../other"}}}))
    assert prepare_agent_state(layout).workspaces["repo"] == str(
        layout.agent_home / "old"
    )

    # Reusing a name from a different source must not inherit that source's grant.
    if placement != "resident":
        peer = (
            replace(layout, source_path=source.with_name("other.too"))
            if placement == "roaming"
            else replace(layout, source_ref="gh:other/check")
        )
        config.write_text(tomlkit.dumps({"workspaces": {key: {"repo": "../external"}}}))
        assert prepare_agent_state(peer).workspaces["repo"] == str(
            layout.agent_home / "old"
        )


def test_root_workspace_override_validates_effective_duplicate_sources(tmp_path):
    layout = AgentLayout.resident(tmp_path, "check")
    layout.home.mkdir(parents=True)
    layout.agent_home.mkdir(parents=True, exist_ok=True)
    layout.program.write_text("flow:\n  pass\n")
    layout.config.write_text('[workspaces]\nrepo = "./repo"\n')
    layout.place_config.write_text(
        tomlkit.dumps(
            {"workspaces": {"check": {"other": str(layout.agent_home / "repo")}}}
        )
    )
    with pytest.raises(ValueError, match="workspace path already configured"):
        prepare_agent_state(layout)


def test_workspace_management_lists_root_grants_and_preserves_root_ownership(tmp_path):
    from toolang.state.config import ConfiguredWorkspaces

    home = tmp_path / "agents/check"
    home.mkdir(parents=True)
    repo = tmp_path / "repo"
    repo.mkdir()
    config = tmp_path / "config.toml"
    config.write_text('[workspaces.check]\nrepo = "./repo"\n')
    configured = ConfiguredWorkspaces(
        home / "config.toml",
        lock_path=home / "edit.lock",
        root_config=config,
        key="check",
    )
    assert configured.list() == {"repo": str(repo)}
    with pytest.raises(ValueError, match="name already exists"):
        configured.add(repo, name="repo")
    with pytest.raises(ValueError, match="is configured in"):
        configured.remove("repo")
    assert config.read_text() == '[workspaces.check]\nrepo = "./repo"\n'


@pytest.mark.parametrize("unrelated", ["catalog", "another-agent"])
def test_workspace_inspection_resolves_only_the_selected_root_policy(
    tmp_path, unrelated
):
    from toolang.state.config import ConfiguredWorkspaces

    home = tmp_path / "agents/check"
    home.mkdir(parents=True)
    config = tmp_path / "config.toml"
    policies: dict[str, object] = {"check": {"repo": "./repo"}}
    document: dict[str, object] = {"workspaces": policies}
    if unrelated == "catalog":
        document["plugin"] = {"model_catalog": {"models_dev": {"path": "\x00"}}}
    else:
        policies["peer"] = "\x00"
    config.write_text(tomlkit.dumps(document))
    configured = ConfiguredWorkspaces(
        home / "config.toml",
        lock_path=home / "edit.lock",
        root_config=config,
        key="check",
    )

    assert configured.list() == {"repo": str(tmp_path / "repo")}
