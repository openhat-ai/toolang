"""Example tool plugins for tests and demos."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..protocols.tool import Tool, ToolPlugin
from ..types.tool import ToolContext
from ..utils.function_tools import create_function_tool, tool


@dataclass(frozen=True, slots=True)
class _ExamplePlugin(ToolPlugin):
    name: str
    description: str | None
    _tools: dict[str, Tool]

    def tools(self) -> Mapping[str, Tool]:
        return dict(self._tools)


def create_example_tool_plugins(
    config: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, ToolPlugin]:
    """Build the example tool set explicitly for tests or demos."""

    plugin_config = dict(config or {})
    return {
        "echo": create_echo_tool(plugin_config.get("echo", {})),
        "math_add": create_math_add_tool(plugin_config.get("math_add", {})),
        "working_tree": create_working_tree_tool(plugin_config.get("working_tree", {})),
    }


def create_echo_tool(config: Mapping[str, Any]) -> ToolPlugin:
    """Build the example echo tool plugin."""

    prefix = str(config.get("prefix", ""))

    @tool(name="echo", description="Echo input text with an optional configured prefix.")
    def echo(text: str, context: ToolContext) -> dict[str, Any]:
        output = f"{prefix}{text}"
        return {
            "text": output,
            "length": len(output),
            "wd": str(context.wd),
        }

    return _ExamplePlugin(
        name="echo",
        description="Example echo tools.",
        _tools={"echo": create_function_tool(echo)},
    )


def create_math_add_tool(config: Mapping[str, Any]) -> ToolPlugin:
    """Build the example math-add tool plugin."""

    offset = _number(config.get("offset", 0))

    @tool(name="add", description="Add a list of numbers and return their sum.")
    def add(values: list[float]) -> dict[str, Any]:
        numbers = [_number(value) for value in values]
        total = offset + sum(numbers)
        return {
            "sum": _normalize_number(total),
            "count": len(numbers),
            "offset": _normalize_number(offset),
        }

    return _ExamplePlugin(
        name="math_add",
        description="Example math tools.",
        _tools={"add": create_function_tool(add)},
    )


def create_working_tree_tool(config: Mapping[str, Any]) -> ToolPlugin:
    """Build the example working-tree tool plugin."""

    max_entries = _limit(config.get("max_entries"), fallback=10)

    @tool(
        name="list",
        description="List files and directories under the current working directory.",
    )
    def list_tree(
        path: str = ".",
        limit: int = 0,
        context: ToolContext | None = None,
    ) -> dict[str, Any]:
        if context is None:
            raise ValueError("tool context is required")
        directory = _resolve_path(context, path)
        if not directory.is_dir():
            raise ValueError("path must resolve to a directory")
        effective_limit = _limit(limit, fallback=max_entries)
        entries = sorted(directory.iterdir(), key=lambda entry: entry.name)
        return {
            "path": _display_path(context.wd.resolve(), directory),
            "entries": [
                {"name": entry.name, "kind": "dir" if entry.is_dir() else "file"}
                for entry in entries[:effective_limit]
            ],
            "truncated": len(entries) > effective_limit,
        }

    return _ExamplePlugin(
        name="working_tree",
        description="Example working-tree tools.",
        _tools={"list": create_function_tool(list_tree)},
    )


def _number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("values must contain only numbers")
    return float(value)


def _normalize_number(value: float) -> int | float:
    whole = int(value)
    return whole if value == whole else value


def _limit(value: object, *, fallback: int) -> int:
    if value in (None, 0):
        return fallback
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("limit must be an integer")
    if value < 1:
        raise ValueError("limit must be at least 1")
    return value


def _resolve_path(context: ToolContext, path_value: str) -> Path:
    root = context.wd.resolve()
    resolved = (root / Path(path_value)).resolve()
    if resolved != root and root not in resolved.parents:
        raise ValueError("path escapes the working directory")
    return resolved


def _display_path(root: Path, directory: Path) -> str:
    if directory == root:
        return "."
    return str(directory.relative_to(root))
