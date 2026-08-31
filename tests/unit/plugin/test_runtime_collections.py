from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

import pytest

from toolang.base.errors import ToolangError
from toolang.base.types.model import ModelInfo, ModelTarget
from toolang.base.types.tool import ToolContext, ToolDefinition
from toolang.plugin.models.collections import ModelCollection, ModelEntry
from toolang.plugin.toolsets.collections import ToolCollection


class _Tool:
    plugin_name = "test"

    def __init__(self, toolset: str, name: str) -> None:
        self.toolset = toolset
        self.name = name

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=f"Use {self.name}.",
            parameters={"type": "object"},
        )

    async def invoke(
        self,
        arguments: Mapping[str, Any],
        context: ToolContext,
    ) -> dict[str, Any]:
        del arguments, context
        return {}


def _model_entry(
    provider: str,
    model: str,
    *,
    key: str | None = None,
    tools: bool = True,
) -> ModelEntry:
    ref = f"{provider}/{model}"
    info = ModelInfo(
        ref=ref,
        provider=provider,
        name=model,
        model=model,
        tools=tools,
    )
    return ModelEntry(
        key=key or ref,
        ref=ref,
        target=ModelTarget(
            ref=ref,
            provider=provider,
            name=model,
            model=model,
            adapter="test",
            tools=tools,
        ),
        info=info,
    )


def test_model_collection_owns_matching_set_operations_and_exact_indexes() -> None:
    alpha = _model_entry("alpha", "one")
    beta = _model_entry("beta", "two", tools=False)
    gamma = _model_entry("alpha", "three")
    models = ModelCollection((alpha, beta, gamma))

    assert models.match(("beta/*", "alpha/*")).refs() == (
        "alpha/one",
        "beta/two",
        "alpha/three",
    )
    assert models.match("*[tool_call=false]").refs() == ("beta/two",)
    assert models.apply(
        (
            ("-=", "alpha/*"),
            ("+=", "alpha/three"),
            ("=", "*[tool_call]"),
        )
    ).refs() == ("alpha/three",)
    assert models.match("missing/*").refs() == ()
    assert models.resolve("beta/two") is beta
    assert models.entry("alpha/one") is alpha
    assert models.subset(("alpha/three", "alpha/one")).entries == (gamma, alpha)
    assert models.contains("alpha/one")
    assert not models.contains("missing/model")
    with pytest.raises(TypeError):
        cast(dict[str, object], alpha.info.metadata)["mutable"] = True
    with pytest.raises(TypeError):
        cast(dict[str, object], alpha.target.options)["mutable"] = True
    with pytest.raises(ToolangError, match="model ref is unavailable"):
        models.resolve("missing/model")


def test_model_collection_keys_are_stable_and_duplicate_refs_are_rejected() -> None:
    first = ModelCollection((_model_entry("alpha", "one"), _model_entry("beta", "two")))
    rebuilt = ModelCollection(
        (_model_entry("alpha", "one"), _model_entry("beta", "two"))
    )

    assert first.keys() == rebuilt.keys() == ("alpha/one", "beta/two")
    assert first == rebuilt
    with pytest.raises(ValueError, match="duplicate public refs"):
        ModelCollection(
            (
                _model_entry("alpha", "one", key="route-a"),
                _model_entry("alpha", "one", key="route-b"),
            )
        )


def test_tool_collection_owns_matching_set_operations_and_exact_indexes() -> None:
    alpha = _Tool("alpha", "one")
    beta = _Tool("beta", "two")
    tools = ToolCollection.from_tools(
        {"alpha__one": alpha, "beta__two": beta},
    )

    assert tools.refs() == ("alpha/one", "beta/two")
    assert tools.match(("beta/*", "alpha/*")).refs() == (
        "alpha/one",
        "beta/two",
    )
    assert tools.apply(
        (("-=", "alpha/*"), ("+=", "alpha/*"), ("=", "alpha/*"))
    ).refs() == ("alpha/one",)
    assert tools.match("missing/*").refs() == ()
    assert tools.resolve("beta/two").tool is beta
    assert tools.entry("beta__two").tool is beta
    assert tools.contains("alpha/one")
    assert not tools.contains("missing/tool")
    with pytest.raises(ToolangError, match="tool ref is unavailable"):
        tools.resolve("missing/tool")


def test_tool_collection_keys_are_stable_and_duplicate_refs_are_rejected() -> None:
    first_tools = {
        "alpha__one": _Tool("alpha", "one"),
        "beta__two": _Tool("beta", "two"),
    }
    rebuilt_tools = {
        "alpha__one": _Tool("alpha", "one"),
        "beta__two": _Tool("beta", "two"),
    }

    first = ToolCollection.from_tools(first_tools)
    rebuilt = ToolCollection.from_tools(rebuilt_tools)

    assert (
        tuple(entry.key for entry in first.entries)
        == tuple(entry.key for entry in rebuilt.entries)
        == ("alpha__one", "beta__two")
    )
    assert first.refs() == rebuilt.refs()
    with pytest.raises(ValueError, match="duplicate public refs"):
        ToolCollection.from_tools(
            {
                "route-a": _Tool("alpha", "one"),
                "route-b": _Tool("alpha", "one"),
            }
        )
