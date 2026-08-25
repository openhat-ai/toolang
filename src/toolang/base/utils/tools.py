"""Shared tool helper functions."""

from __future__ import annotations

import re

from ..errors import ToolangError

_TOOL_NAME_PART = re.compile(r"^[A-Za-z0-9_]+$")


def encode_tool_name(namespace: str, tool_name: str) -> str:
    """Return one model-facing tool name for one namespaced leaf tool."""

    _require_tool_name_part(namespace, kind="namespace")
    _require_tool_name_part(tool_name, kind="tool")
    return f"{namespace}__{tool_name}"


def join_tool_name(toolset_name: str, tool_name: str) -> str:
    """Return one model-facing tool name for one toolset leaf tool."""

    return encode_tool_name(toolset_name, tool_name)


def _require_tool_name_part(value: str, *, kind: str) -> None:
    if not _TOOL_NAME_PART.fullmatch(value):
        raise ToolangError(
            f"{kind} name must contain only letters, numbers, and underscores: {value!r}"
        )
