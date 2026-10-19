"""Root discovery shares list semantics and does not mutate agent homes."""

import shutil

from toolang.common.layout import AgentLayout
from toolang.up.discovery import agent_layouts, agent_roster


def test_discovery_tracks_directories_without_writing_to_them(tmp_path):
    layout = AgentLayout.resident(tmp_path, "alice")
    layout.home.mkdir(parents=True)
    (layout.home.parent / "not-a-home").write_text("ignored")
    assert agent_layouts(tmp_path) == (layout,)
    assert agent_roster(tmp_path) == {"agent:alice"}
    assert not list(layout.home.iterdir())
    shutil.rmtree(layout.home)
    assert agent_roster(tmp_path) == set()
    assert not layout.home.exists()


def test_selected_discovery_tracks_removal_without_enumerating_peers(
    tmp_path, monkeypatch
):
    layout = AgentLayout.resident(tmp_path, "alice")
    layout.home.mkdir(parents=True)

    def no_enumeration(path):
        raise AssertionError("selected discovery must not enumerate peers")

    monkeypatch.setattr(type(tmp_path), "iterdir", no_enumeration)
    assert agent_layouts(tmp_path, agent="alice") == (layout,)
    shutil.rmtree(layout.home)
    assert agent_layouts(tmp_path, agent="alice") == ()
    assert not layout.home.exists()
