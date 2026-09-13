"""Internal resource failures translated at the tool boundary."""

from typing import Any

from toolang.base.errors import ToolangError
from toolang.base.types.tool import ToolResult


class ResourceError(ToolangError):
    def __init__(self, message: str, output: dict[str, Any]):
        super().__init__(message)
        self.result = ToolResult(output, message)
