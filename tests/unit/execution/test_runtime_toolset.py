"""Runtime tools use standard registration and isolated invocation authority."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest

from toolang.base.errors import ToolangError
from toolang.base.types.run import ToolCall
from toolang.base.types.tool import ToolContext
from toolang.execution.records import step_given_from_data, step_given_to_data
from toolang.execution.types import ToolStepGiven
from toolang.plugin.toolsets.collections import ToolCollection
from toolang.plugin.toolsets.loading import load_tools


def test_installed_runtime_toolset_has_no_old_aliases_or_future_tools() -> None:
    tools = ToolCollection.from_tools(load_tools())
    assert set(tools.runtime) == {
        "_toolang__run",
        "_toolang__execute",
        "_toolang__reload",
    }
    assert set(tools.user).isdisjoint(tools.runtime)
    assert "me__get" in tools.user
    assert not any(name.startswith(("_too__", "_me__")) for name in tools)
    for name, tool in tools.runtime.items():
        assert tool.definition().name == name
        assert getattr(tool, "source") == "built-in"


class _Runtime:
    def __init__(self, marker: str):
        self.marker = marker
        self.calls = []

    async def run(self, runnable, input):
        self.calls.append((runnable, dict(input)))
        await asyncio.sleep(0)
        return {"run_id": self.marker, "output_type": "Text", "output": input["_"]}

    async def execute(self, runnable, input):
        self.calls.append((runnable, dict(input)))
        await asyncio.sleep(0)
        return {"controls": [self.marker]}

    async def reload(self):
        self.calls.append("reload")
        await asyncio.sleep(0)
        return {"controls": [self.marker]}


@pytest.mark.parametrize("name", ["run", "execute", "reload"])
def test_shared_plugin_keeps_per_call_authority_isolated(
    tmp_path: Path, name: str
) -> None:
    tool = load_tools()[f"_toolang__{name}"]
    first, second = _Runtime("first"), _Runtime("second")
    context = ToolContext("run_first", tmp_path, tmp_path, tmp_path, runtime=first)
    arguments = (
        {} if name == "reload" else {"runnable": "child", "input": {"_": "input"}}
    )

    async def scenario():
        return await asyncio.gather(
            tool.invoke(arguments, context),
            tool.invoke(
                arguments, replace(context, run_id="run_second", runtime=second)
            ),
        )

    results = asyncio.run(scenario())
    assert first.calls == second.calls
    assert len(first.calls) == 1
    if name == "run":
        assert [result["run_id"] for result in results] == ["first", "second"]
    else:
        assert results == [{"controls": ["first"]}, {"controls": ["second"]}]


@pytest.mark.parametrize(
    "name, arguments",
    [
        ("reload", {"run_id": "another"}),
        ("run", {"runnable": "child", "step": "another"}),
        ("execute", {"runnable": "child", "input": []}),
        ("run", {"runnable": ""}),
    ],
)
def test_runtime_arguments_cannot_supply_authority(
    tmp_path: Path, name, arguments
) -> None:
    runtime = _Runtime("unused")
    context = ToolContext("run_owner", tmp_path, tmp_path, tmp_path, runtime=runtime)
    with pytest.raises(ToolangError):
        asyncio.run(load_tools()[f"_toolang__{name}"].invoke(arguments, context))
    assert runtime.calls == []


@pytest.mark.parametrize("trigger", ["model", "runtime"])
def test_tool_trigger_roundtrips_in_the_record_codec(trigger) -> None:
    given = ToolStepGiven(
        "test", ToolCall("call", "provider", "test__run", {}), trigger=trigger
    )
    data = step_given_to_data("tool", given)
    assert data["trigger"] == trigger
    assert step_given_from_data("tool", data) == given
    with pytest.raises(ValueError, match="trigger"):
        step_given_from_data("tool", {**data, "trigger": "argument"})
    del data["trigger"]
    with pytest.raises(ValueError, match="trigger"):
        step_given_from_data("tool", data)
