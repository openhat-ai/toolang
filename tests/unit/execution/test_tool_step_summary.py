from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import pytest

from toolang.base.types.run import ToolCall
from toolang.base.types.tool import ToolContext, ToolDefinition
from toolang.base.utils.function_tools import (
    create_function_tool,
    tool as function_tool,
)
from toolang.plugin.toolsets.loading import LoadedTool
from toolang.plugin.toolsets.registry import ToolRef
from toolang.plugin.toolsets.filesystem import FilesystemToolset
from toolang.execution.executor.steps.tool import (
    _tool_summary,
    _tool_summary_context,
)


@dataclass(frozen=True, slots=True)
class _Tool:
    parameters: dict[str, Any]
    name: str = "demo__call"

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description="Test tool.",
            parameters=self.parameters,
        )

    async def invoke(
        self,
        arguments: Mapping[str, Any],
        context: ToolContext,
    ) -> dict[str, Any]:
        del arguments, context
        return {}


def _call(input: dict[str, Any]) -> ToolCall:
    return ToolCall("tool-1", "call-1", "demo__call", input)


def test_default_summary_uses_schema_order_instead_of_call_input_order() -> None:
    tool = _Tool(
        {
            "type": "object",
            "properties": {
                "first": {"type": "string"},
                "second": {"type": "string"},
            },
        }
    )

    context = _tool_summary_context(
        _call({"second": "later", "first": "primary value"}),
        tool,
    )

    assert context.family == "demo"
    assert context.name == "call"
    assert context.args == ("“primary value”", "later")
    assert _tool_summary(context, "running") == "Executing call “primary value” ..."
    assert _tool_summary(context, "canceled") == "Canceled call “primary value”"


@pytest.mark.parametrize(
    ("name", "schema"),
    [
        ("api_token", {"type": "string"}),
        ("payload", {"type": "string", "writeOnly": True}),
        ("passcode", {"type": "string", "format": "password"}),
    ],
)
def test_default_summary_redacts_sensitive_argument(
    name: str,
    schema: dict[str, Any],
) -> None:
    tool = _Tool(
        {
            "type": "object",
            "properties": {name: schema},
        }
    )

    context = _tool_summary_context(_call({name: "do-not-show"}), tool)

    assert context.args == ("<redacted>",)
    assert _tool_summary(context, "failed") == "Failed call <redacted>"


def test_default_summary_compacts_and_bounds_argument_preview() -> None:
    tool = _Tool(
        {
            "type": "object",
            "properties": {"query": {"type": "string"}},
        }
    )

    context = _tool_summary_context(_call({"query": "word\n" * 40}), tool)
    preview = context.args[0]

    assert "\n" not in preview
    assert len(preview) <= 80
    assert preview.startswith("“word word")
    assert preview.endswith("…”")


def test_default_summary_omits_argument_without_a_schema_property() -> None:
    tool = _Tool({"type": "object"})

    context = _tool_summary_context(_call({"value": 3}), tool)

    assert context.family == "demo"
    assert context.name == "call"
    assert context.args == ()
    assert _tool_summary(context, "succeeded") == "Executed call"


@pytest.mark.parametrize("status", ["running", "succeeded", "failed", "canceled"])
def test_description_flows_through_factory_and_loaded_tool_without_mutating_data(
    status,
):
    seen = []

    def describe(arguments, status, output):
        seen.append((arguments["password"], arguments["credential"], status))
        arguments["items"].append("changed")
        if output is not None:
            output["items"].append("changed")
        return f"Custom {status}\n description"

    @function_tool(
        name="call",
        description="Unchanged model guidance.",
        parameters={
            "properties": {
                "items": {"type": "array"},
                "password": {"type": "string"},
                "credential": {"writeOnly": True},
            }
        },
        describe=describe,
    )
    def call_tool(items, password, credential):
        return {"items": items}

    tool = LoadedTool(
        "demo",
        "built-in",
        ToolRef("demo", "demo", "call"),
        create_function_tool(call_tool),
    )
    call = _call({"items": ["original"], "password": "secret", "credential": "secret"})
    output = {"items": ["result"]}
    context = _tool_summary_context(call, tool)
    assert _tool_summary(context, status, output) == f"Custom {status} description"
    assert _tool_summary(context, status, output) == f"Custom {status} description"
    assert seen == [("<redacted>", "<redacted>", status)] * 2
    assert call.input == {
        "items": ["original"],
        "password": "secret",
        "credential": "secret",
    }
    assert output == {"items": ["result"]}
    assert tool.definition().description == "Unchanged model guidance."


@pytest.mark.parametrize("outcome", [None, "", "   ", "raises"])
def test_description_failure_or_empty_value_uses_generic_fallback(outcome, caplog):
    def describe(arguments, status, output):
        if outcome == "raises":
            raise ValueError("never log this secret")
        return outcome

    @function_tool(describe=describe)
    def call(value: str):
        return {}

    context = _tool_summary_context(
        _call({"value": "original"}), create_function_tool(call)
    )
    assert _tool_summary(context, "succeeded") == "Executed call original"
    assert "never log this secret" not in caplog.text


@pytest.mark.parametrize(
    "arguments",
    [
        {"path": "workspace://repo/%zz"},
        {"workspace": "repo", "path": "workspace://repo/file"},
        {"path": 1},
        {},
    ],
)
def test_invalid_workspace_input_can_still_be_described_as_a_failure(arguments):
    call = ToolCall("tool-1", "call-1", "fs__read", arguments)
    context = _tool_summary_context(call, FilesystemToolset({}).tools()["read"])
    assert _tool_summary(context, "failed").startswith("Failed read")
    assert call.input == arguments
