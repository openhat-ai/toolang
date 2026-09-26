from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import fields
from pathlib import Path
from threading import Lock
from typing import Any, cast

import pytest

import toolang.setup as setup_package
from toolang.base.types.model import Model, ModelToolang, Provider
from toolang.base.types.policy import RunDefaults, RunLimits
from toolang.common.layout import AgentLayout
from toolang.plugin.toolsets.collections import ToolCollection
from toolang.setup import AgentEnvironment, AgentSetup
from toolang.setup.types import _ModelData


def test_setup_facade_exposes_no_catalog_projection_cache() -> None:
    assert setup_package.__all__ == [
        "AgentEnvironment",
        "AgentSetup",
        "ModelCollection",
        "RunDefaults",
        "SetupWatcher",
        "ToolCollection",
        "ToolEntry",
    ]
    assert not hasattr(setup_package, "ModelListCache")
    assert not hasattr(setup_package, "prepare_agent_setup")
    assert "model_catalog" not in AgentSetup.__dict__
    assert "model_listing" not in AgentSetup.__dict__


def test_agent_setup_fields_are_a_lazy_revision_facade() -> None:
    assert tuple(item.name for item in fields(AgentSetup)) == (
        "layout",
        "envs",
        "revision",
        "environment",
        "defaults",
        "limits",
        "compact_model",
        "catalog_sources",
        "_load_models",
        "_load_tools",
        "_allowed_tools",
        "_load_adapters",
        "_load_catalogs",
        "_load_toolset_plugins",
        "_lazy",
    )


@pytest.mark.parametrize("invalid", [-1, 4, 7])
def test_model_status_rejects_unknown_bits(invalid: int) -> None:
    with pytest.raises(ValueError, match="unknown bits"):
        ModelToolang(provider="test", status=invalid)


def test_run_defaults_require_a_typed_model_request() -> None:
    with pytest.raises(TypeError, match="run default model must be a ModelRequest"):
        RunDefaults(model=cast(Any, "test/model"))


def test_agent_setup_copies_and_freezes_captured_inputs() -> None:
    environ = {"OPENAI_API_KEY": "secret"}
    catalog_sources = {"models_dev": ("models_dev", "sha256:source")}
    setup = AgentSetup(
        layout=AgentLayout.resident(Path("/toolang"), "alice"),
        envs=environ,
        catalog_sources=catalog_sources,
    )
    environ.clear()
    catalog_sources.clear()
    assert setup.envs == {"OPENAI_API_KEY": "secret"}
    assert setup.catalog_sources == {"models_dev": ("models_dev", "sha256:source")}
    with pytest.raises(TypeError):
        cast(dict[str, str], setup.envs)["OTHER"] = "value"
    with pytest.raises(TypeError):
        cast(dict[str, tuple[str, str]], setup.catalog_sources)["other"] = (
            "other",
            "sha256:other",
        )
    assert setup.defaults == RunDefaults()
    assert setup.limits == RunLimits()


def test_accessors_are_lazy_single_flight_and_revision_local() -> None:
    model = Model(
        id="one",
        name="One",
        _toolang=ModelToolang(provider="test", ready=True),
    )
    provider = Provider(id="test", name="Test")
    lock = Lock()
    calls = {"models": 0, "tools": 0, "toolsets": 0, "adapters": 0, "catalogs": 0}

    def load_models(_setup: AgentSetup) -> _ModelData:
        with lock:
            calls["models"] += 1
        models = (model,)
        providers = (provider,)
        return _ModelData(
            models=models,
            providers=providers,
            models_effective=models,
            providers_effective=providers,
        )

    def load_toolset_plugins():
        calls["toolsets"] += 1
        return {}

    def load_tools(_plugins) -> ToolCollection:
        calls["tools"] += 1
        return ToolCollection()

    def load_adapters():
        calls["adapters"] += 1
        return {"test": cast(Any, object())}

    def load_catalogs():
        calls["catalogs"] += 1
        return {}

    setup = AgentSetup(
        layout=AgentLayout.resident(Path("/toolang"), "alice"),
        envs={},
        _load_models=load_models,
        _load_tools=load_tools,
        _load_toolset_plugins=load_toolset_plugins,
        _load_adapters=load_adapters,
        _load_catalogs=load_catalogs,
    )
    assert calls == {key: 0 for key in calls}
    with ThreadPoolExecutor(max_workers=8) as pool:
        values = tuple(pool.map(lambda _: setup.models(), range(8)))
    assert calls["models"] == 1
    assert all(value is values[0] for value in values)
    assert setup.providers() == (provider,)
    assert setup.models_effective() is values[0]
    assert setup.model_allowed(model.ref)
    assert not setup.tools()
    assert tuple(setup.adapters()) == ("test",)
    assert not setup.catalogs()
    assert calls == {
        "models": 1,
        "tools": 1,
        "toolsets": 1,
        "adapters": 1,
        "catalogs": 1,
    }


def test_agent_environment_captures_safe_sandbox_context(tmp_path: Path) -> None:
    layout = AgentLayout.resident(tmp_path, "alice")
    environment = AgentEnvironment.capture(layout, sandbox="docker:python:3.13-slim")
    assert environment.sandbox == "docker:python:3.13-slim"
    assert environment.container is True
    assert environment.root == layout.root
    assert environment.home == layout.home
    assert environment.system
    assert environment.machine


def test_toolsets_accessor_does_not_construct_leaf_tools():
    from collections.abc import Mapping
    from toolang.base.protocols.tool import Tool
    from toolang.base.types.tool import ToolDefinition, ToolResult
    from toolang.plugin.toolsets.loading import tools_from_toolsets
    from toolang.plugin.types import LoadedPlugin

    class Leaf(Tool):
        name = "echo"

        def definition(self) -> ToolDefinition:
            return ToolDefinition(name=self.name, description="Echo.")

        async def invoke(self, arguments, context) -> ToolResult:
            return ToolResult(dict(arguments))

    class Plugin:
        name = "alpha"
        description = None

        def __init__(self):
            self.calls = 0

        def tools(self) -> Mapping[str, Tool]:
            self.calls += 1
            return {"echo": Leaf()}

    plugin = Plugin()
    loaded = {
        "alpha": LoadedPlugin(
            entry_point_name="alpha",
            name="alpha",
            plugin=plugin,
            source="built-in",
        )
    }
    setup = AgentSetup(
        layout=AgentLayout.resident(Path("/toolang"), "alice"),
        envs={},
        _load_toolset_plugins=lambda: loaded,
        _load_tools=lambda plugins: ToolCollection.from_tools(
            tools_from_toolsets(plugins)
        ),
    )

    assert setup.toolsets()["alpha"] is plugin
    assert plugin.calls == 0
    tools = setup.tools()
    assert tuple(tools) == ("alpha__echo",)
    assert setup.tools() is tools
    assert plugin.calls == 1


def test_failed_lazy_load_publishes_no_partial_value_and_can_retry():
    calls = 0

    def load_models(_setup: AgentSetup) -> _ModelData:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("temporary catalog failure")
        return _ModelData(
            models=(),
            providers=(),
            models_effective=(),
            providers_effective=(),
        )

    setup = AgentSetup(
        layout=AgentLayout.resident(Path("/toolang"), "alice"),
        envs={},
        _load_models=load_models,
    )
    with pytest.raises(RuntimeError, match="temporary catalog failure"):
        setup.models()
    assert not setup.models()
    assert calls == 2
