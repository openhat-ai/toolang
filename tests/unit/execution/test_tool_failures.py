from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from toolang.base.errors import ToolFailure
from toolang.base.types.run import ToolCall
from toolang.base.types.tool import ToolContext, ToolDefinition
from toolang.common.layout import AgentLayout
from toolang.execution.executor.steps.tool import invoke_tool_call


@dataclass(frozen=True, slots=True)
class _FailingTool:
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
    ) -> dict[str, Any]:
        del arguments, context
        raise ToolFailure(
            "request is invalid",
            output={"error": {"code": "invalid_request", "issues": []}},
        )


def test_generic_tool_dispatch_preserves_structured_failure_output(
    tmp_path: Path,
) -> None:
    tool = _FailingTool()
    result = asyncio.run(
        invoke_tool_call(
            run_id="run-test",
            tools={tool.name: tool},
            services=(),
            layout=AgentLayout.resident(tmp_path, "alice"),
            call=ToolCall("tool-1", "call-1", tool.name, {}),
        )
    )

    assert result.error == "request is invalid"
    assert result.output == {"error": {"code": "invalid_request", "issues": []}}
