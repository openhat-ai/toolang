from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

import pytest

from toolang.base.errors import ToolangError
from toolang.base.types.tool import ToolContext, ToolDefinition, ToolResult
from toolang.plugin.toolsets.collections import ToolCollection
from tq import Query
from toolang.base.protocols.tool import Tool


class _Tool(Tool):
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
    ) -> ToolResult:
        del arguments, context
        return ToolResult({})


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


def test_tool_collection_exact_subsets_do_not_build_queries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    alpha = _Tool("alpha", "one")
    beta = _Tool("beta", "two")
    tools = ToolCollection.from_tools(
        {"alpha__one": alpha, "beta__two": beta},
    )

    def fail_query_parse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("exact subset must not parse queries")

    monkeypatch.setattr(Query, "parse", fail_query_parse)

    assert tools.subset(("beta__two",)).refs() == ("beta/two",)


def test_tool_collection_public_state_is_immutable() -> None:
    tools = ToolCollection.from_tools({"alpha__one": _Tool("alpha", "one")})

    with pytest.raises(AttributeError):
        setattr(cast(Any, tools), "entries", ())
    with pytest.raises(TypeError):
        cast(dict[str, object], tools._by_ref)["other/tool"] = object()


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
