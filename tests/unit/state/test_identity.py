"""A State revision identifies captured configuration and effective resources."""

import asyncio
from hashlib import sha256
from pathlib import Path

import pytest
import tomlkit

from toolang.common.layout import AgentLayout
from toolang.state import collections
from toolang.state.cache import (
    agent_current_path,
    agent_revision_dir,
    canonical_json,
    load_home_layer,
)
from toolang.state.config import ConfiguredWorkspaces
from toolang.state.prepare import load_agent_state, prepare_agent_state
from toolang.state.source import SourceManifest
from toolang.state.watcher import StateWatcher


def _layout(root: Path) -> AgentLayout:
    layout = AgentLayout.resident(root, "alice")
    layout.home.mkdir(parents=True)
    layout.program.write_text("agic chat:\n  Hello.\n", encoding="utf-8")
    return layout


@pytest.mark.parametrize("change", ["add", "remove", "remap"])
def test_workspace_changes_are_captured_by_revision(tmp_path, monkeypatch, change):
    layout = _layout(tmp_path)
    original = {"one": str(tmp_path / "one")}
    changed = {
        "add": {**original, "two": str(tmp_path / "two")},
        "remove": {},
        "remap": {"one": str(tmp_path / "replacement")},
    }[change]
    layout.config.write_text(tomlkit.dumps({"workspaces": original}))
    before = prepare_agent_state(layout)
    layout.config.write_text(tomlkit.dumps({"workspaces": changed}))

    # Preparation/publication must use the captured config, not the CLI loader.
    monkeypatch.setattr(
        ConfiguredWorkspaces, "list", lambda _self: pytest.fail("live config read")
    )
    after = prepare_agent_state(layout)
    assert after.revision != before.revision
    assert after.workspaces == changed
    assert before.workspaces == original

    layout.config.write_text("invalid toml = [")
    restored = load_agent_state(layout, before.revision)
    assert restored == before
    assert restored.workspaces == original


def test_full_config_bytes_affect_revision_without_copying_unowned_content(tmp_path):
    layout = _layout(tmp_path)
    content = '[models.providers.test]\napi_key = "fake-test-credential"\n'
    layout.config.write_text(content)
    before = prepare_agent_state(layout)
    layout.config.write_text(
        content + "\n# A dependency changed, but no State terms did.\n"
    )
    after = prepare_agent_state(layout)

    assert before.revision != after.revision
    assert before.home_revision != after.home_revision
    assert before.modules == after.modules
    assert before.caps_by_module == after.caps_by_module == {"agent": ()}
    assert before.config == after.config == {}
    layer = load_home_layer(layout, before.home_revision)
    assert isinstance(layer.source, SourceManifest)
    config = next(item for item in layer.source.files if item.path == "config.toml")
    assert config.digest == sha256(content.encode()).hexdigest()
    assert config.size == len(content.encode())
    assert not (layer.revision_dir / "files/config.toml").exists()
    assert "fake-test-credential" not in (layer.revision_dir / "layer.json").read_text()


def test_startup_cap_replacements_have_distinct_reproducible_revisions(
    tmp_path, monkeypatch
):
    layout = _layout(tmp_path)
    prompts = layout.home / "prompts"
    prompts.mkdir()
    for name in ("one", "two"):
        (prompts / f"{name}.md").write_text(name)
    layout.config.write_text('[allow]\nprompts = ["prompt/one"]\n')
    configured = prepare_agent_state(layout)
    replacement = prepare_agent_state(
        layout, allow_overrides={"prompts": ("prompt/two",)}
    )
    denied = prepare_agent_state(layout, allow_overrides={"prompts": ()})
    assert len({configured.revision, replacement.revision, denied.revision}) == 3
    assert configured.home_revision == replacement.home_revision == denied.home_revision
    assert [cap.name for cap in configured.caps_for("agent")] == ["one"]
    assert [cap.name for cap in replacement.caps_for("agent")] == ["two"]
    assert denied.caps_for("agent") == ()

    # A new process uses the recorded overrides, not its own policy or live config.
    watcher = StateWatcher(layout, allow_overrides={"prompts": ()})
    layout.config.unlink()
    assert watcher.load(replacement.revision) == replacement
    assert watcher.load(configured.revision) == configured

    def no_query(*_args, **_kwargs):
        pytest.fail("unchanged State must not recompute effective caps")

    layout.config.write_text('[allow]\nprompts = ["prompt/one"]\n')
    monkeypatch.setattr(collections, "cap_dataset", no_query)
    assert asyncio.run(watcher.refresh()) is watcher.current()
    assert watcher.current() == denied


def test_unchanged_refresh_reuses_effective_caps(tmp_path, monkeypatch):
    layout = _layout(tmp_path)
    (layout.home / "prompts").mkdir()
    (layout.home / "prompts/one.md").write_text("One.")
    layout.config.write_text('[allow]\nprompts = ["prompt/one"]\n')
    watcher = StateWatcher(layout)
    state = asyncio.run(watcher.refresh())

    def no_query(*_args, **_kwargs):
        pytest.fail("unchanged State must not recompute effective caps")

    monkeypatch.setattr(collections, "cap_dataset", no_query)
    assert asyncio.run(watcher.refresh()) is state
    assert watcher.load(state.revision) is state
    assert state.caps_for("agent") is state.caps_for("agent")


def test_old_composition_is_rejected_and_current_cache_is_rebuilt(tmp_path):
    layout = _layout(tmp_path)
    current = prepare_agent_state(layout)
    encoded = canonical_json(
        {
            "schema": 1,
            "root_revision": current.root_revision,
            "home_revision": current.home_revision,
        }
    )
    legacy = sha256(encoded).hexdigest()
    directory = agent_revision_dir(layout, legacy)
    directory.mkdir()
    (directory / "layers.json").write_bytes(encoded)
    agent_current_path(layout).write_text(legacy)

    with pytest.raises(ValueError, match="unsupported Agent State schema"):
        load_agent_state(layout, legacy)
    restored = asyncio.run(StateWatcher(layout).refresh())
    assert restored == current
    assert (directory / "layers.json").read_bytes() == encoded
