"""Internal resource failures translated at the tool boundary."""

from typing import Any

from toolang.base.errors import ToolangError
from toolang.base.types.tool import ToolResult


class ResourceError(ToolangError):
    def __init__(self, message: str, output: dict[str, Any]):
        super().__init__(message)
        self.result = ToolResult(output, message)


class UnsafeAuthoringPathError(ValueError):
    """A target cannot be reached within the declared home file boundary."""


class DigestMismatchError(ValueError):
    def __init__(self, expected: str, actual: str) -> None:
        self.expected = expected
        self.actual = actual
        super().__init__("file changed since it was read; get it again before editing")
