"""Shell tool plugin."""

from __future__ import annotations

import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from toolang.base.errors import ToolangError
from toolang.base.protocols.tool import AgentTool, AgentToolSet
from toolang.base.types.tool import ToolContext
from toolang.base.utils.function_tools import create_function_tool, tool

DEFAULT_TIMEOUT_SEC = 30
DEFAULT_MAX_OUTPUT_CHARS = 20_000


@dataclass(slots=True)
class ShellPlugin:
    """Shell execution tools scoped to one agent home."""

    config: dict[str, Any]
    name: str = "shell"
    description: str | None = (
        "Run non-interactive shell commands inside the current agent home."
    )
    _timeout_sec: int = field(init=False, repr=False)
    _max_output_chars: int = field(init=False, repr=False)
    _tools: dict[str, AgentTool] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._timeout_sec = _int_value(
            self.config.get("timeout_sec"), default=DEFAULT_TIMEOUT_SEC
        )
        self._max_output_chars = _int_value(
            self.config.get("max_output_chars"),
            default=DEFAULT_MAX_OUTPUT_CHARS,
        )
        self._tools = self._build_tools()

    def tools(self) -> Mapping[str, AgentTool]:
        return dict(self._tools)

    def _build_tools(self) -> dict[str, AgentTool]:
        @tool(
            name="execute",
            description="Run one shell command and capture stdout and stderr.",
        )
        def execute(
            command: str,
            cwd: str | None = None,
            timeout_sec: int = self._timeout_sec,
            max_output_chars: int = self._max_output_chars,
            context: ToolContext | None = None,
        ) -> dict[str, Any]:
            resolved_cwd = _resolve_cwd(cwd, context=context)
            timeout = _int_value(timeout_sec, default=self._timeout_sec)
            output_limit = _int_value(max_output_chars, default=self._max_output_chars)
            try:
                completed = subprocess.run(
                    ["/bin/sh", "-lc", command],
                    cwd=str(resolved_cwd),
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise ToolangError(f"shell command timed out after {timeout}s") from exc
            return {
                "cwd": str(resolved_cwd),
                "exit_code": completed.returncode,
                "ok": completed.returncode == 0,
                "stdout": completed.stdout[:output_limit],
                "stderr": completed.stderr[:output_limit],
                "stdout_truncated": len(completed.stdout) > output_limit,
                "stderr_truncated": len(completed.stderr) > output_limit,
            }

        return {"execute": create_function_tool(execute)}


def create_tool_set(config: Mapping[str, Any]) -> AgentToolSet:
    """Create the shell tool plugin."""

    return ShellPlugin(config=dict(config))


def _resolve_cwd(raw_cwd: str | None, *, context: ToolContext | None) -> Path:
    if context is None:
        raise ToolangError("shell tool context is required")
    text = str(raw_cwd or "").strip()
    if not text:
        return context.wd.resolve()
    candidate = Path(text).expanduser()
    if not candidate.is_absolute():
        candidate = context.wd / candidate
    resolved = candidate.resolve()
    root = context.home.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ToolangError(f"shell cwd escapes agent home: {resolved}") from exc
    return resolved


def _int_value(value: object, *, default: int) -> int:
    if value is None:
        return default
    try:
        parsed = (
            value
            if isinstance(value, int) and not isinstance(value, bool)
            else int(str(value))
        )
    except (TypeError, ValueError) as exc:
        raise ToolangError("shell integer argument is invalid") from exc
    if parsed <= 0:
        raise ToolangError("shell integer argument must be positive")
    return parsed
