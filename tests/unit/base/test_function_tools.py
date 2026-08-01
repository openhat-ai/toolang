from __future__ import annotations

import asyncio
from pathlib import Path
import threading

from toolang.base.types.tool import ToolContext
from toolang.base.utils.function_tools import create_function_tool, tool


def _context(home: Path) -> ToolContext:
    return ToolContext(
        run_id="run-1",
        home=home,
        room=home / ".runtime" / "tools" / "test",
        wd=home,
    )


def test_function_tool_runs_sync_callable_in_worker_thread(tmp_path: Path) -> None:
    owner_thread = threading.get_ident()

    @tool()
    def current_thread() -> dict[str, int]:
        return {"thread": threading.get_ident()}

    result = asyncio.run(
        create_function_tool(current_thread).invoke({}, _context(tmp_path))
    )

    assert result["thread"] != owner_thread


def test_function_tool_awaits_async_callable_on_owner_loop(tmp_path: Path) -> None:
    owner_thread = threading.get_ident()

    @tool()
    async def current_thread() -> dict[str, int]:
        await asyncio.sleep(0)
        return {"thread": threading.get_ident()}

    result = asyncio.run(
        create_function_tool(current_thread).invoke({}, _context(tmp_path))
    )

    assert result["thread"] == owner_thread
