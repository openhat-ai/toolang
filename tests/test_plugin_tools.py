from __future__ import annotations

from toolang.experiments.up import list_plugin_names, load_plugin_factory, load_tool_plugins


class _FakeEntryPoint:
    def __init__(self, name: str, target) -> None:
        self.name = name
        self._target = target

    def load(self):
        return self._target


def _patch_tool_entry_points(monkeypatch) -> None:
    from toolang.experiments.base.examples.tools import create_echo_tool
    from toolang.experiments.base.examples.tools import create_math_add_tool
    from toolang.experiments.tools.filesystem import create_tool as create_filesystem_tool
    from toolang.experiments.tools.service_use import create_tool as create_service_use_tool
    from toolang.experiments.tools.shell import create_tool as create_shell_tool
    from toolang.experiments.tools.web_search import create_tool as create_web_search_tool
    from toolang.experiments.base.examples.tools import create_working_tree_tool

    entries = [
        _FakeEntryPoint("echo", create_echo_tool),
        _FakeEntryPoint("filesystem", create_filesystem_tool),
        _FakeEntryPoint("math_add", create_math_add_tool),
        _FakeEntryPoint("service_use", create_service_use_tool),
        _FakeEntryPoint("shell", create_shell_tool),
        _FakeEntryPoint("web_search", create_web_search_tool),
        _FakeEntryPoint("working_tree", create_working_tree_tool),
    ]
    monkeypatch.setattr(
        "toolang.experiments.up.entry_points",
        lambda *, group: entries if group == "toolang.tool" else [],
    )

def test_tool_plugins_load_from_entry_points(monkeypatch) -> None:
    _patch_tool_entry_points(monkeypatch)

    assert list_plugin_names(group="toolang.tool") == [
        "echo",
        "filesystem",
        "math_add",
        "service_use",
        "shell",
        "web_search",
        "working_tree",
    ]


def test_load_plugin_factory_loads_named_factory(monkeypatch) -> None:
    _patch_tool_entry_points(monkeypatch)

    factory = load_plugin_factory("echo", group="toolang.tool")

    assert factory.__name__ == "create_echo_tool"


def test_load_tool_plugins_uses_hyphenated_model_names(monkeypatch) -> None:
    _patch_tool_entry_points(monkeypatch)

    tools = load_tool_plugins()

    assert "shell_execute" in tools
    assert "web_search_search" in tools
    assert "service_use_bridge_start" in tools
    assert "service_use_init" in tools
    assert "service_use_auth_start" in tools
    assert "service_use_tool_call" in tools
    assert tools["service_use_bridge_start"].definition().name == "service_use_bridge_start"
    assert tools["service_use_init"].definition().name == "service_use_init"
    assert tools["service_use_auth_start"].definition().name == "service_use_auth_start"
    assert tools["service_use_tool_call"].definition().name == "service_use_tool_call"
