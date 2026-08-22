from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import pytest

from toolang.base.types.run import ToolCall
from toolang.base.types.tool import ToolContext, ToolDefinition
from toolang.execution.executor.steps.tool import _tool_summary_target


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

    assert (
        _tool_summary_target(
            _call({"second": "later", "first": "primary value"}),
            tool,
        )
        == "demo__call “primary value”"
    )


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

    assert _tool_summary_target(_call({name: "do-not-show"}), tool) == (
        "demo__call <redacted>"
    )


def test_default_summary_compacts_and_bounds_argument_preview() -> None:
    tool = _Tool(
        {
            "type": "object",
            "properties": {"query": {"type": "string"}},
        }
    )

    target = _tool_summary_target(_call({"query": "word\n" * 40}), tool)
    preview = target.removeprefix("demo__call ")

    assert "\n" not in target
    assert len(preview) <= 80
    assert preview.startswith("“word word")
    assert preview.endswith("…”")


def test_default_summary_omits_argument_without_a_schema_property() -> None:
    tool = _Tool({"type": "object"})

    assert _tool_summary_target(_call({"value": 3}), tool) == "demo__call"
