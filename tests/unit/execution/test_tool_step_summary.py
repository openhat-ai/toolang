from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import pytest

from toolang.base.types.run import ToolCall
from toolang.base.types.tool import ToolContext, ToolDefinition
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
    assert _tool_summary(context, "running") == "calling call “primary value”"


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
    assert _tool_summary(context, "failed") == "failed to call call <redacted>"


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
    assert _tool_summary(context, "succeeded") == "called call"
