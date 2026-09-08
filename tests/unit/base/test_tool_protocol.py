"""The public tool contract has one result and two optional query hooks."""

import asyncio
from dataclasses import fields, replace
from pathlib import Path

import pytest

from toolang.base.protocols.tool import Tool
from toolang.base.types.tool import ToolContext, ToolDefinition, ToolResult
from toolang.base.utils.function_tools import create_function_tool, tool
from toolang.plugin.toolsets.loading import load_tools


class Echo(Tool):
    name = "echo"

    def definition(self):
        return ToolDefinition(self.name, "Echo input.")

    async def invoke(self, arguments, context):
        return ToolResult(dict(arguments))


def test_default_hooks_and_result_contract(tmp_path):
    echo = Echo()
    context = ToolContext(tmp_path, tmp_path)
    assert echo.summary({}) is None
    assert echo.summary({}, ToolResult()) is None
    assert echo.touchpoints({}, context) is None
    assert asyncio.run(echo.invoke({"value": 1}, context)) == ToolResult({"value": 1})
    assert {f.name for f in fields(ToolResult)} == {"output", "error"}
    first, second = ToolResult(), ToolResult()
    first.output["value"] = 1
    assert second.output == {}


def test_function_adapter_preserves_explicit_partial_failure(tmp_path):
    receipt = ToolResult(
        {"processed": 3}, error="The remaining items could not be read."
    )

    @tool()
    def batch():
        return receipt

    assert (
        asyncio.run(
            create_function_tool(batch).invoke({}, ToolContext(tmp_path, tmp_path))
        )
        is receipt
    )


@pytest.mark.parametrize("points", [None, {}, {"repo": ("/src",)}])
def test_function_hooks_use_the_tool_contract_without_defaulting_arguments(
    tmp_path, points
):
    observed = []

    def touchpoints(arguments, context):
        observed.append((dict(arguments), context))
        return points

    @tool(
        touchpoints=touchpoints,
        summary=lambda arguments, result: "Done" if result else "Working",
    )
    def work(path="/src"):
        return {"path": path}

    wrapped = create_function_tool(work)
    context = ToolContext(tmp_path, tmp_path)
    arguments = {}
    assert wrapped.touchpoints(arguments, context) == points
    assert observed == [({}, context)]
    assert wrapped.summary(arguments) == "Working"
    result = asyncio.run(wrapped.invoke(arguments, context))
    assert result == ToolResult({"path": "/src"})
    assert wrapped.summary(arguments, result) == "Done"
    assert arguments == {}


def test_context_is_small_and_captures_workspace_grants(tmp_path):
    roots = {"repo": tmp_path / "first"}
    context = ToolContext(tmp_path, tmp_path, roots)
    roots["repo"] = tmp_path / "second"
    assert context.workspaces["repo"] == tmp_path / "first"
    with pytest.raises(TypeError):
        context.workspaces["extra"] = tmp_path  # type: ignore[invalid-assignment]
    assert {f.name for f in fields(context) if not f.name.startswith("_")} == {
        "home",
        "room",
        "workspaces",
    }
    assert not any(
        hasattr(context, name)
        for name in ("run_id", "wd", "runtime", "history", "services", "placement")
    )


def test_non_path_tools_do_not_inspect_workspace_filesystem(tmp_path, monkeypatch):
    def no_io(*args, **kwargs):
        raise AssertionError("unrelated tools must not resolve workspace roots")

    monkeypatch.setattr(Path, "resolve", no_io)
    roots = {"repo": tmp_path / "unavailable"}
    context = ToolContext(tmp_path, tmp_path, roots)
    assert context.workspaces == roots
    assert Echo().touchpoints({}, context) is None


def test_calls_share_tools_but_not_resolved_targets(tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    old = ToolContext(tmp_path, tmp_path, {"repo": first})
    new = replace(old, workspaces={"repo": second})
    write = load_tools(queries=("fs/write",))["fs__write"]
    old_input = {"workspace": "repo", "path": "file", "text": "old"}
    new_input = {**old_input, "text": "new"}
    assert write.touchpoints(old_input, old) == write.touchpoints(new_input, new)

    async def invoke():
        return await asyncio.gather(
            write.invoke(old_input, old), write.invoke(new_input, new)
        )

    results = asyncio.run(invoke())
    assert all(result.error is None for result in results)
    assert (first / "file").read_text() == "old"
    assert (second / "file").read_text() == "new"


def test_workspace_root_alias_is_bound_for_one_call(tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (first / "file").write_text("first")
    alias = tmp_path / "alias"
    alias.symlink_to(first, target_is_directory=True)
    context = ToolContext(tmp_path, tmp_path, {"repo": alias})
    listing = load_tools(queries=("fs/list",))["fs__list"]
    arguments = {"workspace": "repo", "path": "/"}
    assert listing.touchpoints(arguments, context) == {"repo": ("/",)}
    alias.unlink()
    alias.symlink_to(second, target_is_directory=True)
    result = asyncio.run(listing.invoke(arguments, context))
    assert result.error is None
    assert result.output["entries"] == [
        {"name": "file", "path": "workspace://repo/file", "is_dir": False}
    ]
    assert (
        asyncio.run(listing.invoke(arguments, replace(context))).output["entries"] == []
    )
