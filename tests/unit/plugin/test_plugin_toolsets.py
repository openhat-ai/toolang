from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import pytest

from toolang.base.errors import ToolangError
from toolang.base.protocols.model import ModelAdapter
from toolang.base.protocols.tool import AgentTool, Toolset
from toolang.base.types.model import ModelCatalogSnapshot, ModelTarget
from toolang.base.types.run import ModelCall, ModelCallResult
from toolang.base.types.tool import ToolContext, ToolDefinition
from toolang.base.utils.function_tools import create_function_tool, tool
from toolang.plugin.models.loading import load_model_adapters, load_model_catalogs
from toolang.plugin.toolsets.registry import ToolRef
from toolang.plugin.loading import (
    PluginInfo,
    list_plugin_infos,
    list_plugin_names,
    load_plugin_factory,
)
from toolang.plugin.toolsets.loading import (
    load_tools,
    load_toolsets,
    validate_tool_selectors,
)


@dataclass(frozen=True, slots=True)
class _FakeDistribution:
    metadata: Mapping[str, str]


class _FakeEntryPoint:
    def __init__(
        self,
        name: str,
        target,
        *,
        value: str | None = None,
        distribution: str | None = None,
    ) -> None:
        self.name = name
        self._target = target
        self.value = value
        self.dist = (
            _FakeDistribution({"Name": distribution})
            if distribution is not None
            else None
        )

    def load(self):
        return self._target


@dataclass(frozen=True, slots=True)
class _TestTool(AgentTool):
    name: str

    def definition(self) -> ToolDefinition:
        return ToolDefinition(name=self.name, description="Test tool.")

    async def invoke(
        self,
        arguments: Mapping[str, Any],
        context: ToolContext,
    ) -> dict[str, Any]:
        del arguments, context
        return {}


@dataclass(frozen=True, slots=True)
class _TestToolset(Toolset):
    name: str
    key: str
    leaf_name: str
    description: str | None = None

    def tools(self) -> Mapping[str, AgentTool]:
        return {self.key: _TestTool(self.leaf_name)}


def _test_toolset_factory(toolset_name: str, key: str, leaf_name: str):
    def create_toolset(config: Mapping[str, Any]) -> Toolset:
        del config
        return _TestToolset(toolset_name, key, leaf_name)

    return create_toolset


def _patch_tool_entry_points(monkeypatch) -> None:
    from toolang.base.examples.tools import create_echo_toolset
    from toolang.base.examples.tools import create_math_add_toolset
    from toolang.plugin.toolsets.filesystem import (
        create_toolset as create_filesystem_tool,
    )
    from toolang.plugin.toolsets.service_use import (
        create_toolset as create_service_use_tool,
    )
    from toolang.plugin.toolsets.shell import create_toolset as create_shell_tool
    from toolang.plugin.toolsets.web_search import (
        create_toolset as create_web_search_tool,
    )
    from toolang.execution.tools.agent_state import (
        create_toolset as create_agent_state_tool,
    )
    from toolang.execution.tools.runtime import create_toolset as create_runtime_tool
    from toolang.base.examples.tools import create_working_tree_toolset

    entries = [
        _FakeEntryPoint("_me", create_agent_state_tool, distribution="toolang"),
        _FakeEntryPoint("_too", create_runtime_tool, distribution="toolang"),
        _FakeEntryPoint("echo", create_echo_toolset),
        _FakeEntryPoint("fs", create_filesystem_tool, distribution="toolang"),
        _FakeEntryPoint("math_add", create_math_add_toolset),
        _FakeEntryPoint("service", create_service_use_tool, distribution="toolang"),
        _FakeEntryPoint("shell", create_shell_tool, distribution="toolang"),
        _FakeEntryPoint("web", create_web_search_tool, distribution="toolang"),
        _FakeEntryPoint("working_tree", create_working_tree_toolset),
    ]
    monkeypatch.setattr(
        "toolang.plugin.loading.entry_points",
        lambda *, group: entries if group == "toolang.toolset" else [],
    )


def test_toolsets_load_from_entry_points(monkeypatch) -> None:
    _patch_tool_entry_points(monkeypatch)

    assert list_plugin_names(group="toolang.toolset") == [
        "_me",
        "_too",
        "echo",
        "fs",
        "math_add",
        "service",
        "shell",
        "web",
        "working_tree",
    ]


def test_plugin_infos_include_source(monkeypatch) -> None:
    from toolang.base.examples.tools import create_echo_toolset
    from toolang.plugin.toolsets.filesystem import (
        create_toolset as create_filesystem_tool,
    )

    entries = [
        _FakeEntryPoint(
            "echo", create_echo_toolset, value="demo.tools:create_echo_toolset"
        ),
        _FakeEntryPoint(
            "fs",
            create_filesystem_tool,
            value="toolang.plugin.toolsets.filesystem:create_toolset",
            distribution="toolang",
        ),
        _FakeEntryPoint(
            "spoof",
            create_echo_toolset,
            value="toolang.external:create_toolset",
        ),
    ]
    monkeypatch.setattr(
        "toolang.plugin.loading.entry_points",
        lambda *, group: entries if group == "toolang.toolset" else [],
    )

    assert list_plugin_infos(group="toolang.toolset") == [
        PluginInfo(name="echo", source="external"),
        PluginInfo(name="fs", source="built-in"),
        PluginInfo(name="spoof", source="external"),
    ]


def test_load_plugin_factory_loads_named_factory(monkeypatch) -> None:
    _patch_tool_entry_points(monkeypatch)

    factory = load_plugin_factory("echo", group="toolang.toolset")

    assert factory.__name__ == "create_echo_toolset"


def test_load_tools_uses_encoded_model_names(monkeypatch) -> None:
    _patch_tool_entry_points(monkeypatch)

    tools = load_tools()

    expected = {
        "fs__list",
        "fs__read",
        "fs__write",
        "fs__append",
        "fs__glob",
        "fs__stat",
        "fs__mkdir",
        "fs__remove",
        "web__search",
        "shell__execute",
        "service__start_bridge",
        "service__stop_bridge",
        "service__init",
        "service__start_auth",
        "service__complete_auth",
        "service__list_tools",
        "service__call_tool",
        "service__list_resources",
        "service__list_resource_templates",
        "service__read_resource",
        "service__list_prompts",
        "service__get_prompt",
        "_me__list_tasks",
        "_me__get_task",
        "_me__create_task",
        "_me__update_task",
        "_me__list_chores",
        "_me__get_chore",
        "_me__create_chore",
        "_me__update_chore",
        "_me__list_psyches",
        "_me__get_psyche",
        "_me__create_psyche",
        "_me__update_psyche",
        "_me__delete_psyche",
        "_me__list_skills",
        "_me__get_skill",
        "_me__create_skill",
        "_me__update_skill",
        "_me__delete_skill",
        "_me__list_services",
        "_me__get_service",
        "_me__create_service",
        "_me__update_service",
        "_me__delete_service",
        "_me__list_prompts",
        "_me__get_prompt",
        "_me__create_prompt",
        "_me__update_prompt",
        "_me__delete_prompt",
        "_too__reload",
        "_too__run",
    }

    assert expected <= tools.keys()
    assert tools["service__start_bridge"].definition().name == "service__start_bridge"
    assert tools["service__init"].definition().name == "service__init"
    assert tools["service__start_auth"].definition().name == "service__start_auth"
    assert tools["service__call_tool"].definition().name == "service__call_tool"
    assert getattr(tools["_too__run"], "ref") == ToolRef(
        plugin="_too",
        toolset="_too",
        name="run",
    )
    assert (
        not {
            "fs__read_text",
            "fs__write_text",
            "fs__append_text",
        }
        & tools.keys()
    )
    assert not any(
        name.startswith(
            ("filesystem__", "web_search__", "service_use__", "agent_state__")
        )
        for name in tools
    )


def test_canonical_tool_selectors_include_internal_toolset(monkeypatch) -> None:
    _patch_tool_entry_points(monkeypatch)
    tools = load_tools()

    selected = load_tools(selectors=("fs/read", "service/call_tool", "_me/*"))

    assert "fs__read" in selected
    assert "service__call_tool" in selected
    assert "_me__create_task" in selected
    validate_tool_selectors(tools, ("fs/*", "service/call_tool", "_me/*"))
    with pytest.raises(ValueError, match="tool selector matched no tools"):
        validate_tool_selectors(tools, ("filesystem/*",))


def test_load_tools_accepts_explicit_toolset_keys(monkeypatch) -> None:
    @tool(name="search", description="Search issues.")
    def search() -> dict[str, object]:
        return {}

    @tool(name="create", description="Create issues.")
    def create() -> dict[str, object]:
        return {}

    @dataclass(frozen=True, slots=True)
    class Plugin(Toolset):
        name: str = "tracker"
        description: str | None = None

        def tools(self) -> Mapping[str, AgentTool]:
            return {
                "issues/search": create_function_tool(search),
                "issues/create": create_function_tool(create),
            }

    def create_toolset(config: Mapping[str, Any]) -> Toolset:
        del config
        return Plugin()

    monkeypatch.setattr(
        "toolang.plugin.loading.entry_points",
        lambda *, group: (
            [_FakeEntryPoint("tracker", create_toolset)]
            if group == "toolang.toolset"
            else []
        ),
    )

    tools = load_tools()

    assert sorted(tools) == ["issues__create", "issues__search"]
    assert getattr(tools["issues__search"], "ref") == ToolRef(
        plugin="tracker", toolset="issues", name="search"
    )


def test_toolang_distribution_can_register_an_internal_toolset(monkeypatch) -> None:
    entry = _FakeEntryPoint(
        "_me",
        _test_toolset_factory("_me", "create_task", "create_task"),
        distribution="toolang",
    )
    monkeypatch.setattr(
        "toolang.plugin.loading.entry_points",
        lambda *, group: [entry] if group == "toolang.toolset" else [],
    )

    tools = load_tools()

    assert tuple(tools) == ("_me__create_task",)
    assert getattr(tools["_me__create_task"], "ref") == ToolRef(
        plugin="_me", toolset="_me", name="create_task"
    )


def test_builtin_toolset_precedes_and_rejects_an_external_name_collision(
    monkeypatch,
) -> None:
    calls: list[str] = []

    def recording_factory(label: str, key: str):
        factory = _test_toolset_factory("fs", key, key)

        def create_toolset(config: Mapping[str, Any]) -> Toolset:
            calls.append(label)
            return factory(config)

        return create_toolset

    entries = [
        _FakeEntryPoint(
            "tracker",
            recording_factory("external", "capture"),
        ),
        _FakeEntryPoint(
            "fs",
            recording_factory("built-in", "read"),
            distribution="toolang",
        ),
    ]
    monkeypatch.setattr(
        "toolang.plugin.loading.entry_points",
        lambda *, group: entries if group == "toolang.toolset" else [],
    )

    with pytest.raises(
        ToolangError,
        match=(
            "duplicate toolset plugin name 'fs': built-in entry point 'fs' "
            "conflicts with external entry point 'tracker'"
        ),
    ):
        load_tools()
    assert calls == ["built-in", "external"]


@pytest.mark.parametrize("toolset", ["_me", "_too", "_hat", "_private"])
def test_external_plugin_cannot_register_internal_toolset(
    monkeypatch,
    toolset: str,
) -> None:
    entry = _FakeEntryPoint(
        "tracker",
        _test_toolset_factory("tracker", f"{toolset}/run", "run"),
    )
    monkeypatch.setattr(
        "toolang.plugin.loading.entry_points",
        lambda *, group: [entry] if group == "toolang.toolset" else [],
    )

    with pytest.raises(ToolangError, match="cannot register internal toolset"):
        load_tools()


def test_toolang_module_target_does_not_grant_internal_authority(monkeypatch) -> None:
    entry = _FakeEntryPoint(
        "_me",
        _test_toolset_factory("_me", "create_task", "create_task"),
        value="toolang.external:create_toolset",
    )
    monkeypatch.setattr(
        "toolang.plugin.loading.entry_points",
        lambda *, group: [entry] if group == "toolang.toolset" else [],
    )

    with pytest.raises(ToolangError, match="toolset plugin name"):
        load_tools()


def test_external_internal_entry_point_cannot_hide_behind_public_name(
    monkeypatch,
) -> None:
    entry = _FakeEntryPoint(
        "_me",
        _test_toolset_factory("tracker", "run", "run"),
    )
    monkeypatch.setattr(
        "toolang.plugin.loading.entry_points",
        lambda *, group: [entry] if group == "toolang.toolset" else [],
    )

    with pytest.raises(ToolangError, match="toolset plugin name"):
        load_tools()


@pytest.mark.parametrize(
    "plugin_name",
    [
        "",
        "_private",
        "tool1",
        "tool-name",
        "tool.name",
        "tool__name",
        "tool/name",
        " tool",
        "tool ",
        "tool name",
        "工具",
    ],
)
def test_external_toolset_rejects_invalid_effective_plugin_name(
    monkeypatch,
    plugin_name: str,
) -> None:
    entry = _FakeEntryPoint(
        plugin_name,
        _test_toolset_factory(plugin_name, "run", "run"),
    )
    monkeypatch.setattr(
        "toolang.plugin.loading.entry_points",
        lambda *, group: [entry] if group == "toolang.toolset" else [],
    )

    with pytest.raises(ToolangError, match="toolset plugin name"):
        load_tools()


@pytest.mark.parametrize(
    "toolset",
    [
        "",
        "tools1",
        "tool-name",
        "tool.name",
        "tool__name",
        "tool/name",
        " tools",
        "tools ",
        "tool name",
        "工具",
    ],
)
def test_external_plugin_rejects_invalid_public_toolset(
    monkeypatch,
    toolset: str,
) -> None:
    entry = _FakeEntryPoint(
        "tracker",
        _test_toolset_factory("tracker", f"{toolset}/run", "run"),
    )
    monkeypatch.setattr(
        "toolang.plugin.loading.entry_points",
        lambda *, group: [entry] if group == "toolang.toolset" else [],
    )

    with pytest.raises(ToolangError):
        load_tools()


@pytest.mark.parametrize(
    "leaf_name",
    [
        "",
        "_run",
        "run1",
        "run-now",
        "run.now",
        "run__now",
        "run/now",
        " run",
        "run ",
        "run now",
        "运行",
    ],
)
def test_toolset_rejects_invalid_leaf_name(monkeypatch, leaf_name: str) -> None:
    entry = _FakeEntryPoint(
        "tracker",
        _test_toolset_factory("tracker", leaf_name, leaf_name),
    )
    monkeypatch.setattr(
        "toolang.plugin.loading.entry_points",
        lambda *, group: [entry] if group == "toolang.toolset" else [],
    )

    with pytest.raises(ToolangError):
        load_tools()


def test_one_python_package_can_define_multiple_toolang_plugins(monkeypatch) -> None:
    adapter_configs: list[dict[str, Any]] = []

    @tool(name="alpha", description="Alpha.")
    def alpha() -> dict[str, object]:
        return {}

    @tool(name="beta", description="Beta.")
    def beta() -> dict[str, object]:
        return {}

    @dataclass(frozen=True, slots=True)
    class DemoToolset(Toolset):
        name: str
        description: str | None
        _tools: Mapping[str, AgentTool]

        def tools(self) -> Mapping[str, AgentTool]:
            return self._tools

    def create_alpha_toolset(config: Mapping[str, Any]) -> Toolset:
        del config
        return DemoToolset("alpha", None, {"alpha": create_function_tool(alpha)})

    def create_beta_toolset(config: Mapping[str, Any]) -> Toolset:
        del config
        return DemoToolset("beta", None, {"beta": create_function_tool(beta)})

    @dataclass(frozen=True, slots=True)
    class Adapter(ModelAdapter):
        name: str = "package_adapter"
        description: str | None = None
        default_api: str | None = "https://example.invalid/v1"

        async def invoke(
            self,
            target: ModelTarget,
            request: ModelCall,
        ) -> ModelCallResult:
            del target, request
            return ModelCallResult()

        async def stream(
            self, target: ModelTarget, request: ModelCall, *, on_event
        ) -> ModelCallResult:
            del on_event
            return await self.invoke(target, request)

    def create_model_adapter(config: Mapping[str, Any]) -> ModelAdapter:
        adapter_configs.append(dict(config))
        return Adapter()

    entry_points_by_group = {
        "toolang.toolset": [
            _FakeEntryPoint(
                "alpha",
                create_alpha_toolset,
                value="demo.plugins:create_alpha_toolset",
            ),
            _FakeEntryPoint(
                "beta", create_beta_toolset, value="demo.plugins:create_beta_toolset"
            ),
        ],
        "toolang.model_adapter": [
            _FakeEntryPoint(
                "package_adapter",
                create_model_adapter,
                value="demo.plugins:create_model_adapter",
            ),
        ],
    }
    monkeypatch.setattr(
        "toolang.plugin.loading.entry_points",
        lambda *, group: entry_points_by_group.get(group, []),
    )
    toolsets = load_toolsets()
    tools = load_tools()
    adapters = load_model_adapters(
        {"package_adapter": {"endpoint": "https://example.test/v1"}}
    )

    assert sorted(toolsets) == ["alpha", "beta"]
    assert sorted(tools) == ["alpha__alpha", "beta__beta"]
    assert "package_adapter" in adapters
    assert adapter_configs == [{"endpoint": "https://example.test/v1"}]


def test_plugin_factories_receive_fresh_nested_config_mappings(monkeypatch) -> None:
    @dataclass(frozen=True, slots=True)
    class EmptyToolset(Toolset):
        name: str = "mutable"
        description: str | None = None

        def tools(self) -> Mapping[str, AgentTool]:
            return {}

    def create_toolset(config: Mapping[str, Any]) -> Toolset:
        nested = config["nested"]
        assert isinstance(nested, dict)
        nested["changed"] = True
        return EmptyToolset()

    monkeypatch.setattr(
        "toolang.plugin.loading.entry_points",
        lambda *, group: (
            [_FakeEntryPoint("mutable", create_toolset)]
            if group == "toolang.toolset"
            else []
        ),
    )
    source = {"mutable": {"nested": {"changed": False}}}

    load_toolsets(config=source)

    assert source == {"mutable": {"nested": {"changed": False}}}


def test_model_catalog_loader_instantiates_only_configured_plugins(monkeypatch) -> None:
    created: dict[str, dict[str, object]] = {}

    @dataclass(frozen=True)
    class Catalog:
        name: str

        async def snapshot(self) -> ModelCatalogSnapshot:
            return ModelCatalogSnapshot(providers={}, models=(), revision="test")

    def company_factory(config: Mapping[str, Any]) -> Catalog:
        created["company"] = dict(config)
        return Catalog("company")

    def unconfigured_factory(config: Mapping[str, Any]) -> Catalog:
        created["unconfigured"] = dict(config)
        return Catalog("unconfigured")

    entries = [
        _FakeEntryPoint("company", company_factory),
        _FakeEntryPoint("unconfigured", unconfigured_factory),
    ]
    monkeypatch.setattr(
        "toolang.plugin.loading.entry_points",
        lambda *, group: entries if group == "toolang.model_catalog" else [],
    )

    catalogs = load_model_catalogs({"company": {"url": "https://example.test"}})

    assert tuple(catalogs) == ("company",)
    assert created == {"company": {"url": "https://example.test"}}
