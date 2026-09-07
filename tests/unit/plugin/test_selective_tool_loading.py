"""Internal programs load only their declared plugin dependencies."""

from types import SimpleNamespace

import pytest

from toolang.base.errors import ToolangError
from toolang.plugin import loading
from toolang.plugin.toolsets.history import create_toolset
from toolang.plugin.toolsets.loading import load_tools


def test_selected_toolset_does_not_initialize_unrelated_plugins(monkeypatch):
    installed = loading.entry_points(group="toolang.toolset")
    unrelated = SimpleNamespace(
        name="unrelated", load=lambda: pytest.fail("unrelated factory loaded")
    )
    monkeypatch.setattr(
        loading, "entry_points", lambda **kwargs: (*installed, unrelated)
    )
    tools = load_tools(toolsets=("history",))
    assert set(tools) == {
        f"history__{name}"
        for name in ("read_threads", "read_runs", "read_steps", "read_output")
    }


def test_selected_toolset_still_rejects_duplicate_registration(monkeypatch):
    installed = loading.entry_points(group="toolang.toolset")
    duplicate = SimpleNamespace(name="history", load=lambda: create_toolset)
    monkeypatch.setattr(
        loading, "entry_points", lambda **kwargs: (*installed, duplicate)
    )
    with pytest.raises(ToolangError, match="duplicate toolset"):
        load_tools(toolsets=("history",))
