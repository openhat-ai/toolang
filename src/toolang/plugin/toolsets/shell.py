"""Shell toolset plugin."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass, field
import locale
import os
from pathlib import Path
import signal
from typing import Any, cast

from toolang.base.errors import ToolangError
from toolang.base.protocols.tool import AgentTool, Toolset
from toolang.base.types.tool import ToolContext, ToolPath
from toolang.base.utils.function_tools import create_function_tool, tool
from toolang.base.utils.paths import resolve_tool_path

DEFAULT_TIMEOUT_SEC = 30
DEFAULT_MAX_OUTPUT_CHARS = 20_000


@dataclass(slots=True)
class ShellToolset:
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
            description="Run one shell command and capture stdout and stderr. Optional workspace anchors cwd at that workspace root, including paths starting with /.",
            prepare=_prepare_cwd,
        )
        async def execute(
            command: str,
            cwd: str | None = None,
            timeout_sec: int = self._timeout_sec,
            max_output_chars: int = self._max_output_chars,
            workspace: str | None = None,
            context: ToolContext | None = None,
        ) -> dict[str, Any]:
            resolved_cwd = Path(cast(str, cwd))  # Preparation supplies a concrete cwd.
            timeout = _int_value(timeout_sec, default=self._timeout_sec)
            output_limit = _int_value(max_output_chars, default=self._max_output_chars)
            launch = asyncio.create_task(
                asyncio.create_subprocess_exec(
                    "/bin/sh",
                    "-lc",
                    command,
                    cwd=str(resolved_cwd),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    start_new_session=True,
                )
            )
            try:
                process = await asyncio.shield(launch)
                stdout, stderr = await asyncio.wait_for(process.communicate(), timeout)
            except BaseException as exc:
                # Retain ownership even when canceled during spawn or cleanup.
                cleanup = asyncio.create_task(_stop_command(launch))
                while not cleanup.done():
                    try:
                        await asyncio.shield(cleanup)
                    except asyncio.CancelledError:
                        continue
                cleanup.result()
                if isinstance(exc, TimeoutError):
                    raise ToolangError(
                        f"shell command timed out after {timeout}s"
                    ) from exc
                raise
            out = _decode_output(stdout)
            err = _decode_output(stderr)
            return {
                "cwd": str(resolved_cwd),
                "exit_code": process.returncode,
                "ok": process.returncode == 0,
                "stdout": out[:output_limit],
                "stderr": err[:output_limit],
                "stdout_truncated": len(out) > output_limit,
                "stderr_truncated": len(err) > output_limit,
            }

        return {"execute": create_function_tool(execute)}


async def _stop_command(launch: asyncio.Task[asyncio.subprocess.Process]) -> None:
    process = await launch
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    await process.communicate()


def _decode_output(value: bytes) -> str:
    # Match subprocess text mode, including universal newline translation.
    return (
        value.decode(locale.getpreferredencoding(False))
        .replace("\r\n", "\n")
        .replace("\r", "\n")
    )


def create_toolset(config: Mapping[str, Any]) -> Toolset:
    """Create the shell toolset plugin."""

    return ShellToolset(config=dict(config))


def _prepare_cwd(
    arguments: dict[str, Any], context: ToolContext
) -> tuple[ToolPath, ...]:
    value = str(arguments["cwd"] or "").strip() or "."
    path = resolve_tool_path(value, context, workspace=arguments["workspace"])
    arguments["cwd"] = str(path.resolved)
    return (path,)


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
