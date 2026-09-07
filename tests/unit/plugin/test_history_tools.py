"""History plugins only validate arguments and use per-call read authority."""

import asyncio
from dataclasses import replace
from typing import cast

import pytest

from toolang.base.errors import ToolangError
from toolang.base.protocols.tool import ToolHistory
from toolang.base.types.tool import ToolContext
from toolang.plugin.toolsets.collections import ToolCollection
from toolang.plugin.toolsets.loading import load_tools


class Reader:
    def __init__(self, marker):
        self.marker = marker
        self.calls = []

    def __getattr__(self, name):
        def read(**query):
            self.calls.append((name, query))
            return {"reader": self.marker}

        return read


def test_history_is_an_ordinary_selectable_user_toolset():
    tools = ToolCollection.from_tools(load_tools())
    names = {
        f"history__{leaf}"
        for leaf in ("read_threads", "read_runs", "read_steps", "read_output")
    }
    assert names <= set(tools.user)
    assert names.isdisjoint(tools.runtime)
    assert set(load_tools(queries=("history/*",))) == names
    assert names.isdisjoint(load_tools(queries=("fs/*",)))


@pytest.mark.parametrize(
    "name,query",
    [
        ("read_threads", {}),
        ("read_threads", {"cursor": None}),
        (
            "read_runs",
            {
                "thread": "term_a",
                "begin": "run_a",
                "end": "run_z",
                "from_end": True,
                "limit": 2,
            },
        ),
        ("read_steps", {"run": "run_a"}),
        ("read_steps", {"cursor": "opaque"}),
        ("read_output", {"run": "run_a"}),
    ],
)
def test_history_authority_is_per_call(tmp_path, name, query):
    first, second = Reader("first"), Reader("second")
    context = ToolContext(
        "run_a", tmp_path, tmp_path, tmp_path, history=cast(ToolHistory, first)
    )
    tool = load_tools()[f"history__{name}"]

    async def scenario():
        return await asyncio.gather(
            tool.invoke(query, context),
            tool.invoke(query, replace(context, history=cast(ToolHistory, second))),
        )

    assert asyncio.run(scenario()) == [{"reader": "first"}, {"reader": "second"}]
    assert first.calls == second.calls == [(name, query)]


@pytest.mark.parametrize(
    "name,query",
    [
        ("read_threads", {"agent": "other"}),
        ("read_threads", {"limit": 0}),
        ("read_threads", {"limit": -1}),
        ("read_threads", {"limit": True}),
        ("read_threads", {"limit": 1.5}),
        ("read_threads", {"limit": None}),
        ("read_threads", {"cursor": "saved", "limit": 20}),
        ("read_threads", {"cursor": ""}),
        ("read_threads", {"cursor": 1}),
        ("read_runs", {"cursor": "saved", "thread": None}),
        ("read_runs", {"from_end": 1}),
        ("read_runs", {"begin": {"?": "run_a"}}),
        ("read_steps", {}),
        ("read_steps", {"run": None, "cursor": None}),
        ("read_steps", {"run": "run_a", "cursor": "saved"}),
        ("read_output", {}),
        ("read_output", {"run": 1}),
        ("read_output", {"run": "run_a", "cursor": None}),
    ],
)
def test_invalid_query_never_reaches_the_reader(tmp_path, name, query):
    reader = Reader("unused")
    context = ToolContext(
        "run_a", tmp_path, tmp_path, tmp_path, history=cast(ToolHistory, reader)
    )
    with pytest.raises(ToolangError):
        asyncio.run(load_tools()[f"history__{name}"].invoke(query, context))
    assert reader.calls == []


def test_history_requires_executor_supplied_access(tmp_path):
    context = ToolContext("run_a", tmp_path, tmp_path, tmp_path)
    with pytest.raises(ToolangError, match="unavailable"):
        asyncio.run(load_tools()["history__read_threads"].invoke({}, context))
