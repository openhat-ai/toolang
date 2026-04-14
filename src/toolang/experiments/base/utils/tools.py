"""Shared tool helper functions."""

from __future__ import annotations

import re

from ..error import ToolangError

_TOOL_NAME_PART = re.compile(r"^[A-Za-z0-9_]+$")


def join_tool_name(plugin_name: str, tool_name: str) -> str:
    """Return one model-facing tool name for one plugin leaf tool."""

    _require_tool_name_part(plugin_name, kind="plugin")
    _require_tool_name_part(tool_name, kind="tool")
    return f"{plugin_name}_{tool_name}"


def _require_tool_name_part(value: str, *, kind: str) -> None:
    if not _TOOL_NAME_PART.fullmatch(value):
        raise ToolangError(
            f"{kind} name must contain only letters, numbers, and underscores: {value!r}"
        )
