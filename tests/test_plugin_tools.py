from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from toolang.base.protocols.loop import RunContext
from toolang.base.protocols.model import ModelAdapter
from toolang.base.protocols.tool import AgentTool, AgentToolSet
from toolang.base.types.model import ModelTarget
from toolang.base.types.run import ModelCall, ModelCallResult, RunResult
from toolang.base.utils.function_tools import create_function_tool, tool
from toolang.plugin import load_loops
from toolang.up import load_model_adapters
from toolang.tools.registry import ToolRef
from toolang.up import PluginInfo, list_plugin_infos, list_plugin_names, load_plugin_factory, load_tool_plugins


class _FakeEntryPoint:
    def __init__(self, name: str, target, *, value: str | None = None) -> None:
        self.name = name
        self._target = target
        self.value = value

    def load(self):
        return self._target


def _patch_tool_entry_points(monkeypatch) -> None:
    from toolang.base.examples.tools import create_echo_tool_set
    from toolang.base.examples.tools import create_math_add_tool_set
    from toolang.tools.agent_chat import create_tool_set as create_agent_chat_tool
    from toolang.tools.filesystem import create_tool_set as create_filesystem_tool
    from toolang.tools.service_use import create_tool_set as create_service_use_tool
    from toolang.tools.shell import create_tool_set as create_shell_tool
    from toolang.tools.web_search import create_tool_set as create_web_search_tool
    from toolang.tools.agent_state import create_tool_set as create_agent_state_tool
    from toolang.base.examples.tools import create_working_tree_tool_set

    entries = [
        _FakeEntryPoint("agent_chat", create_agent_chat_tool),
        _FakeEntryPoint("agent_state", create_agent_state_tool),
        _FakeEntryPoint("echo", create_echo_tool_set),
        _FakeEntryPoint("filesystem", create_filesystem_tool),
        _FakeEntryPoint("math_add", create_math_add_tool_set),
        _FakeEntryPoint("service_use", create_service_use_tool),
        _FakeEntryPoint("shell", create_shell_tool),
        _FakeEntryPoint("web_search", create_web_search_tool),
        _FakeEntryPoint("working_tree", create_working_tree_tool_set),
    ]
    monkeypatch.setattr(
        "toolang.plugin.entry_points",
        lambda *, group: entries if group == "toolang.tool" else [],
    )


def test_tool_plugins_load_from_entry_points(monkeypatch) -> None:
    _patch_tool_entry_points(monkeypatch)

    assert list_plugin_names(group="toolang.tool") == [
        "agent_chat",
        "agent_state",
        "echo",
        "filesystem",
        "math_add",
        "service_use",
        "shell",
        "web_search",
        "working_tree",
    ]


def test_plugin_infos_include_source(monkeypatch) -> None:
    from toolang.base.examples.tools import create_echo_tool_set
    from toolang.tools.filesystem import create_tool_set as create_filesystem_tool

    entries = [
        _FakeEntryPoint("echo", create_echo_tool_set, value="demo.tools:create_echo_tool_set"),
        _FakeEntryPoint(
            "filesystem",
            create_filesystem_tool,
            value="toolang.tools.filesystem:create_tool_set",
        ),
    ]
    monkeypatch.setattr(
        "toolang.plugin.entry_points",
        lambda *, group: entries if group == "toolang.tool" else [],
    )

    assert list_plugin_infos(group="toolang.tool") == [
        PluginInfo(name="echo", source="external"),
        PluginInfo(name="filesystem", source="built-in"),
    ]


def test_load_plugin_factory_loads_named_factory(monkeypatch) -> None:
    _patch_tool_entry_points(monkeypatch)

    factory = load_plugin_factory("echo", group="toolang.tool")

    assert factory.__name__ == "create_echo_tool_set"


def test_load_tool_plugins_uses_encoded_model_names(monkeypatch) -> None:
    _patch_tool_entry_points(monkeypatch)

    tools = load_tool_plugins()

    assert "shell__execute" in tools
    assert "web_search__search" in tools
    assert "agent_chat__send" in tools
    assert "agent_state__task_create" in tools
    assert "agent_state__chore_create" in tools
    assert "agent_state__skill_create" in tools
    assert "agent_state__service_create" in tools
    assert "service_use__bridge_start" in tools
    assert "service_use__init" in tools
    assert "service_use__auth_start" in tools
    assert "service_use__tool_call" in tools
    assert tools["service_use__bridge_start"].definition().name == "service_use__bridge_start"
    assert tools["service_use__init"].definition().name == "service_use__init"
    assert tools["service_use__auth_start"].definition().name == "service_use__auth_start"
    assert tools["service_use__tool_call"].definition().name == "service_use__tool_call"


def test_load_tool_plugins_accepts_namespaced_plugin_keys(monkeypatch) -> None:
    @tool(name="search", description="Search issues.")
    def search() -> dict[str, object]:
        return {}

    @tool(name="create", description="Create issues.")
    def create() -> dict[str, object]:
        return {}

    @dataclass(frozen=True, slots=True)
    class Plugin(AgentToolSet):
        name: str = "tracker"
        description: str | None = None

        def tools(self) -> Mapping[str, AgentTool]:
            return {
                "issues/search": create_function_tool(search),
                "issues/create": create_function_tool(create),
            }

    def create_tool_set(config: Mapping[str, Any]) -> AgentToolSet:
        del config
        return Plugin()

    monkeypatch.setattr(
        "toolang.plugin.entry_points",
        lambda *, group: [_FakeEntryPoint("tracker", create_tool_set)] if group == "toolang.tool" else [],
    )

    tools = load_tool_plugins()

    assert sorted(tools) == ["issues__create", "issues__search"]
    assert getattr(tools["issues__search"], "ref") == ToolRef(plugin="tracker", namespace="issues", name="search")


def test_one_python_package_can_define_multiple_toolang_plugins(monkeypatch) -> None:
    @tool(name="alpha", description="Alpha.")
    def alpha() -> dict[str, object]:
        return {}

    @tool(name="beta", description="Beta.")
    def beta() -> dict[str, object]:
        return {}

    @dataclass(frozen=True, slots=True)
    class ToolSet(AgentToolSet):
        name: str
        description: str | None
        _tools: Mapping[str, AgentTool]

        def tools(self) -> Mapping[str, AgentTool]:
            return self._tools

    def create_alpha_tool_set(config: Mapping[str, Any]) -> AgentToolSet:
        del config
        return ToolSet("alpha", None, {"alpha": create_function_tool(alpha)})

    def create_beta_tool_set(config: Mapping[str, Any]) -> AgentToolSet:
        del config
        return ToolSet("beta", None, {"beta": create_function_tool(beta)})

    @dataclass(frozen=True, slots=True)
    class Loop:
        name: str = "package_loop"

        def run(self, context: RunContext) -> RunResult:
            del context
            return RunResult(output_text="ok")

    def create_loop(config: Mapping[str, Any]) -> Loop:
        del config
        return Loop()

    @dataclass(frozen=True, slots=True)
    class Adapter(ModelAdapter):
        name: str = "package_adapter"
        description: str | None = None

        def invoke(self, target: ModelTarget, request: ModelCall) -> ModelCallResult:
            del target, request
            return ModelCallResult()

        def stream(self, target: ModelTarget, request: ModelCall, *, on_event) -> ModelCallResult:
            del on_event
            return self.invoke(target, request)

    def create_model_adapter(config: Mapping[str, Any]) -> ModelAdapter:
        del config
        return Adapter()

    entry_points_by_group = {
        "toolang.tool": [
            _FakeEntryPoint("alpha", create_alpha_tool_set, value="demo.plugins:create_alpha_tool_set"),
            _FakeEntryPoint("beta", create_beta_tool_set, value="demo.plugins:create_beta_tool_set"),
        ],
        "toolang.loop": [
            _FakeEntryPoint("package_loop", create_loop, value="demo.plugins:create_loop"),
        ],
        "toolang.model_adapter": [
            _FakeEntryPoint("package_adapter", create_model_adapter, value="demo.plugins:create_model_adapter"),
        ],
    }
    monkeypatch.setattr(
        "toolang.plugin.entry_points",
        lambda *, group: entry_points_by_group.get(group, []),
    )
    tools = load_tool_plugins()
    loops = load_loops()
    adapters = load_model_adapters()

    assert sorted(tools) == ["alpha__alpha", "beta__beta"]
    assert "package_loop" in loops
    assert "package_adapter" in adapters
