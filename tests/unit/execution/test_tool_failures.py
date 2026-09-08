from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from toolang.base.types.run import ToolCall
from toolang.base.types.tool import ToolContext, ToolDefinition, ToolResult
from toolang.execution.executor.steps.tool import invoke_tool_call
from toolang.base.protocols.tool import Tool


@dataclass(frozen=True, slots=True)
class _FailingTool(Tool):
    name: str = "demo__fail"
    plugin_name: str = "demo"

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description="Fail with structured output.",
            parameters={"type": "object"},
        )

    async def invoke(
        self,
        arguments: Mapping[str, Any],
        context: ToolContext,
    ) -> ToolResult:
        del arguments, context
        return ToolResult(
            error="request is invalid",
            output={"error": {"code": "invalid_request", "issues": []}},
        )


def test_generic_tool_dispatch_preserves_structured_failure_output(
    tmp_path: Path,
) -> None:
    tool = _FailingTool()
    call = ToolCall("tool-1", "call-1", tool.name, {})
    context = ToolContext(tmp_path, tmp_path)
    result = asyncio.run(
        invoke_tool_call(
            call=call,
            tool=tool,
            context=context,
        )
    )

    assert result.error == "request is invalid"
    assert result.output == {"error": {"code": "invalid_request", "issues": []}}
