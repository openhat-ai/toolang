from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from toolang.base.types.tool import ToolContext, ToolDefinition
from toolang.base.types.policy import RunDefaults
from toolang.common.errors import ToolangError
from toolang.common.layout import AgentLayout
from toolang.execution.executor import AgentCeiling
from toolang.execution.executor.resources import (
    agent_model_targets,
    apply_agent_ceiling,
    resolve_agent_resources,
    resolve_runnable_resources,
    snapshot_model_selection,
)
from toolang.execution.types import (
    AgentCapResource,
    AgentResources,
    AgentToolResource,
)
from toolang.lang.ast import AgicDecl, Directive, FlowDecl, Span
from toolang.plugin.models.resolution import build_model_collection
from toolang.plugin.toolsets.collections import ToolCollection
from toolang.setup import AgentSetup
from tests.support.execution_harness import FakeModels


class _Tool:
    def __init__(self, toolset: str, name: str) -> None:
        self.toolset = toolset
        self.name = f"{toolset}__{name}"
        self.plugin_name = toolset
        self.definition_calls = 0

    def definition(self) -> ToolDefinition:
        self.definition_calls += 1
        return ToolDefinition(
            name=self.name,
            description=self.name,
            parameters={"type": "object"},
        )

    async def invoke(
        self,
        arguments: Mapping[str, Any],
        context: ToolContext,
    ) -> dict[str, Any]:
        del arguments, context
        return {}


def _directive(name: str, *values: str) -> Directive:
    return Directive(
        name=name,
        operator="=",
        values=values,
        span=Span(line=1),
    )


def _snapshots(tmp_path: Path) -> tuple[AgentSetup, Any, Any]:
    tools = {
        "alpha__one": _Tool("alpha", "one"),
        "beta__two": _Tool("beta", "two"),
    }
    provider = FakeModels(streaming=False)
    providers = {provider.name: provider.catalog_provider()}
    setup = AgentSetup(
        layout=AgentLayout.resident(tmp_path, "alice"),
        providers=providers,
        adapters={},
        models=build_model_collection(
            providers=providers,
            models=provider.list_models(environ={}),
            envs={},
        ),
        tools=ToolCollection.from_tools(tools),
        envs={},
    )
    state = cast(
        Any,
        SimpleNamespace(
            root_config={},
            home_config={},
            caps=(),
        ),
    )
    return setup, state, setup.models


def test_agent_resources_never_filter_setup_snapshot(tmp_path: Path) -> None:
    setup, state, _selection = _snapshots(tmp_path)

    alpha = resolve_agent_resources(
        setup,
        state,
        AgentCeiling(tools=("alpha/*",)),
    )
    beta = resolve_agent_resources(
        setup,
        state,
        AgentCeiling(tools=("beta/*",)),
    )
    no_models = resolve_agent_resources(
        setup,
        state,
        AgentCeiling(models=()),
    )

    assert tuple(setup.tools) == ("alpha__one", "beta__two")
    assert setup.models.refs() == ("test/scripted",)
    assert tuple(item.model_name for item in alpha.tools) == ("alpha__one",)
    assert tuple(item.model_name for item in beta.tools) == ("beta__two",)
    assert no_models.models == ()
    with pytest.raises(TypeError):
        cast(Any, alpha.tools)["beta__two"] = setup.tools["beta__two"]
    assert AgentResources.from_data(alpha.to_data()) == alpha


def test_runtime_tool_narrowing_reuses_setup_collection_matcher(tmp_path: Path) -> None:
    setup, state, selection = _snapshots(tmp_path)
    tools = tuple(cast(_Tool, tool) for tool in setup.tools.values())
    initial_definition_calls = tuple(tool.definition_calls for tool in tools)
    agent = resolve_agent_resources(setup, state, AgentCeiling())

    narrowed = apply_agent_ceiling(
        setup,
        state,
        agent,
        AgentCeiling(tools=("alpha/*",)),
    )
    resolved = resolve_runnable_resources(
        selection,
        runnable=AgicDecl(name="worker", span=Span(line=1)),
        base=narrowed,
        setup=setup,
        state=state,
    )

    assert tuple(item.model_name for item in resolved.tools) == ("alpha__one",)
    assert tuple(tool.definition_calls for tool in tools) == initial_definition_calls


def test_agent_model_default_is_the_selected_candidate_concrete_ref(
    tmp_path: Path,
) -> None:
    setup, state, _selection = _snapshots(tmp_path)
    setup = replace(setup, defaults=RunDefaults(model="test/scripted"))

    default, targets = agent_model_targets(setup, AgentCeiling())

    assert default == targets[0][0]
    assert default == setup.defaults.model


def test_agent_resources_durable_data_round_trips_every_resource_kind() -> None:
    resources = AgentResources(
        models=("test/model",),
        tools=(
            AgentToolResource(
                model_name="_me__create",
                plugin="_me",
                toolset="_me",
                name="create",
            ),
        ),
        caps=(AgentCapResource(kind="skill", name="review", ref="skill:review"),),
    )

    assert AgentResources.from_data(resources.to_data()) == resources


def test_agent_resources_reads_legacy_tool_namespace_field() -> None:
    resources = AgentResources.from_data(
        {
            "models": [],
            "tools": [
                {
                    "model_name": "fs__read",
                    "plugin": "fs",
                    "namespace": "fs",
                    "name": "read",
                }
            ],
            "caps": [],
        }
    )

    assert resources.tools[0].toolset == "fs"
    assert resources.to_data()["tools"] == [
        {
            "model_name": "fs__read",
            "plugin": "fs",
            "toolset": "fs",
            "name": "read",
        }
    ]


@pytest.mark.parametrize(
    "payload",
    (
        {},
        {"models": "test/model", "tools": [], "caps": []},
        {"models": [1], "tools": [], "caps": []},
        {"models": [], "tools": ["invalid"], "caps": []},
        {"models": [], "tools": [], "caps": ["invalid"]},
        {"models": [], "tools": [], "caps": [], "extra": []},
    ),
)
def test_agent_resources_rejects_noncanonical_durable_data(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValueError, match="agent resources"):
        AgentResources.from_data(payload)


def test_agent_ceiling_normalizes_stable_queries() -> None:
    spec = AgentCeiling(
        models=(" openai/gpt-5 ", "openai/gpt-5"),
        tools=None,
        skills=(),
    )

    assert spec.models == ("openai/gpt-5",)
    assert spec.tools is None
    assert spec.skills == ()


def test_agent_ceiling_cannot_expand_empty_agent_resources(
    tmp_path: Path,
) -> None:
    setup, state, _selection = _snapshots(tmp_path)
    agent = resolve_agent_resources(
        setup,
        state,
        AgentCeiling(models=()),
    )

    with pytest.raises(ToolangError, match="no available models"):
        apply_agent_ceiling(
            setup,
            state,
            agent,
            AgentCeiling(models=("test/scripted",)),
        )


def test_flow_resets_resources_while_agics_use_current_flow(
    tmp_path: Path,
) -> None:
    setup, state, _selection = _snapshots(tmp_path)
    agent = resolve_agent_resources(
        setup,
        state,
        AgentCeiling(),
    )
    model_selection = snapshot_model_selection(setup)
    outer = resolve_runnable_resources(
        model_selection,
        runnable=FlowDecl(
            name="outer",
            directives=(_directive("tools", "alpha/*"),),
            span=Span(line=1),
        ),
        base=agent,
        setup=setup,
        state=state,
    )
    blocked_agic = resolve_runnable_resources(
        model_selection,
        runnable=AgicDecl(
            name="blocked",
            directives=(_directive("tools", "beta/*"),),
            span=Span(line=1),
        ),
        base=outer,
        setup=setup,
        state=state,
    )
    inner = resolve_runnable_resources(
        model_selection,
        runnable=FlowDecl(name="inner", span=Span(line=1)),
        base=agent,
        setup=setup,
        state=state,
    )
    inner_agic = resolve_runnable_resources(
        model_selection,
        runnable=AgicDecl(
            name="inner_agic",
            directives=(_directive("tools", "beta/*"),),
            span=Span(line=1),
        ),
        base=inner,
        setup=setup,
        state=state,
    )
    outer_sibling = resolve_runnable_resources(
        model_selection,
        runnable=AgicDecl(name="outer_sibling", span=Span(line=1)),
        base=outer,
        setup=setup,
        state=state,
    )

    assert tuple(item.model_name for item in outer.tools) == ("alpha__one",)
    assert tuple(blocked_agic.tools) == ()
    assert tuple(item.model_name for item in inner.tools) == (
        "alpha__one",
        "beta__two",
    )
    assert tuple(item.model_name for item in inner_agic.tools) == ("beta__two",)
    assert tuple(item.model_name for item in outer_sibling.tools) == ("alpha__one",)
