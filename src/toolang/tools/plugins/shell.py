"""Default shell tool provider."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from toolang.concepts.tools import ToolDefinition
from toolang.errors import ToolangError

from ..contracts import ToolContext, ToolProvider

DEFAULT_TIMEOUT_SEC = 30
DEFAULT_MAX_OUTPUT_CHARS = 20_000


class ShellTool(ToolProvider):
    """Default non-interactive shell tool scoped to one agent home."""

    family = "shell"

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            family="shell",
            name="shell",
            description=(
                "Run one non-interactive shell command within the current agent home "
                "and capture stdout and stderr."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "cwd": {"type": "string"},
                    "timeout_sec": {"type": "integer", "minimum": 1},
                    "max_output_chars": {"type": "integer", "minimum": 1},
                },
                "required": ["command"],
                "additionalProperties": False,
            },
        )

    def invoke(self, arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        command = _required_text(arguments, "command")
        cwd = _resolve_cwd(arguments.get("cwd"), context=context)
        timeout_sec = _int_value(arguments.get("timeout_sec"), default=DEFAULT_TIMEOUT_SEC)
        max_output_chars = _int_value(
            arguments.get("max_output_chars"), default=DEFAULT_MAX_OUTPUT_CHARS
        )
        try:
            completed = subprocess.run(
                ["/bin/sh", "-lc", command],
                cwd=str(cwd),
                capture_output=True,
                text=True,
                timeout=timeout_sec,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ToolangError(f"shell command timed out after {timeout_sec}s") from exc
        return {
            "cwd": str(cwd),
            "exit_code": completed.returncode,
            "ok": completed.returncode == 0,
            "stdout": completed.stdout[:max_output_chars],
            "stderr": completed.stderr[:max_output_chars],
            "stdout_truncated": len(completed.stdout) > max_output_chars,
            "stderr_truncated": len(completed.stderr) > max_output_chars,
        }


def create_shell_tool(config: dict[str, Any]) -> ToolProvider:
    """Create the default shell tool provider."""

    return ShellTool()


def _resolve_cwd(raw_cwd: object, *, context: ToolContext) -> Path:
    if raw_cwd is None:
        return Path(context.working_directory).resolve()
    text = str(raw_cwd).strip()
    if not text:
        return Path(context.working_directory).resolve()
    candidate = Path(text).expanduser()
    if not candidate.is_absolute():
        candidate = Path(context.working_directory) / candidate
    resolved = candidate.resolve()
    home = Path(context.agent.home).resolve()
    try:
        resolved.relative_to(home)
    except ValueError as exc:
        raise ToolangError(f"shell cwd escapes agent home: {resolved}") from exc
    return resolved


def _required_text(arguments: dict[str, Any], name: str) -> str:
    value = str(arguments.get(name, "")).strip()
    if not value:
        raise ToolangError(f"shell tool requires {name!r}")
    return value


def _int_value(value: object, *, default: int) -> int:
    if value is None:
        return default
    try:
        parsed = value if isinstance(value, int) and not isinstance(value, bool) else int(str(value))
    except (TypeError, ValueError) as exc:
        raise ToolangError("shell tool integer argument is invalid") from exc
    if parsed <= 0:
        raise ToolangError("shell tool integer argument must be positive")
    return parsed
