from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from toolang.base.types.tool import ToolContext, ToolDefinition
from toolang.common.layout import AgentLayout
from toolang.execution.executor import CeilingSpec
from toolang.execution.executor.ceiling import (
    resolve_agent_ceiling,
    resolve_run_ceiling,
)
from toolang.lang.ast import AgicDecl, Directive, FlowDecl, Span
from toolang.setup import AgentSetup
from tests.support.execution_harness import FakeModelProvider


class _Tool:
    def __init__(self, namespace: str, name: str) -> None:
        self.namespace = namespace
        self.name = f"{namespace}__{name}"
        self.plugin_name = namespace

    def definition(self) -> ToolDefinition:
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
    provider = FakeModelProvider(streaming=False)
    setup = AgentSetup(
        layout=AgentLayout.resident(tmp_path, "alice"),
        providers={provider.name: provider},
        adapters={},
        models=provider.list_models(environ={}),
        tools=tools,
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
    selection = cast(
        Any,
        SimpleNamespace(
            providers=setup.providers,
            models=setup.models,
            model_aliases={},
            default_models=(),
            envs=setup.envs,
        ),
    )
    return setup, state, selection


def test_agent_ceiling_never_filters_setup_snapshot(tmp_path: Path) -> None:
    setup, state, _selection = _snapshots(tmp_path)

    alpha = resolve_agent_ceiling(
        setup,
        state,
        CeilingSpec(tools=("alpha/*",)),
    )
    beta = resolve_agent_ceiling(
        setup,
        state,
        CeilingSpec(tools=("beta/*",)),
    )
    no_models = resolve_agent_ceiling(
        setup,
        state,
        CeilingSpec(models=()),
    )

    assert tuple(setup.tools) == ("alpha__one", "beta__two")
    assert tuple(model.ref for model in setup.models) == ("test/scripted",)
    assert tuple(alpha.tools) == ("alpha__one",)
    assert tuple(beta.tools) == ("beta__two",)
    assert no_models.models == ()
    with pytest.raises(TypeError):
        cast(Any, alpha.tools)["beta__two"] = setup.tools["beta__two"]


def test_ceiling_spec_normalizes_stable_selector_lists() -> None:
    spec = CeilingSpec(
        models=(" openai/gpt-5 ", "openai/gpt-5"),
        tools=None,
        caps=(),
    )

    assert spec.models == ("openai/gpt-5",)
    assert spec.tools is None
    assert spec.caps == ()


def test_flow_resets_ceiling_while_agics_use_current_flow(
    tmp_path: Path,
) -> None:
    setup, state, selection = _snapshots(tmp_path)
    agent = resolve_agent_ceiling(
        setup,
        state,
        CeilingSpec(),
    )
    outer = resolve_run_ceiling(
        selection,
        executable=FlowDecl(
            name="outer",
            directives=(_directive("tools", "alpha/*"),),
            span=Span(line=1),
        ),
        agent=agent,
        flow=None,
        agent_name="alice",
    )
    blocked_agic = resolve_run_ceiling(
        selection,
        executable=AgicDecl(
            name="blocked",
            directives=(_directive("tools", "beta/*"),),
            span=Span(line=1),
        ),
        agent=agent,
        flow=outer,
        agent_name="alice",
    )
    inner = resolve_run_ceiling(
        selection,
        executable=FlowDecl(name="inner", span=Span(line=1)),
        agent=agent,
        flow=outer,
        agent_name="alice",
    )
    inner_agic = resolve_run_ceiling(
        selection,
        executable=AgicDecl(
            name="inner_agic",
            directives=(_directive("tools", "beta/*"),),
            span=Span(line=1),
        ),
        agent=agent,
        flow=inner,
        agent_name="alice",
    )
    outer_sibling = resolve_run_ceiling(
        selection,
        executable=AgicDecl(name="outer_sibling", span=Span(line=1)),
        agent=agent,
        flow=outer,
        agent_name="alice",
    )

    assert tuple(outer.tools) == ("alpha__one",)
    assert tuple(blocked_agic.tools) == ()
    assert tuple(inner.tools) == ("alpha__one", "beta__two")
    assert tuple(inner_agic.tools) == ("beta__two",)
    assert tuple(outer_sibling.tools) == ("alpha__one",)
