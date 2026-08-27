"""Shared tool helper functions."""

from __future__ import annotations

import re

from ..errors import ToolangError

_PUBLIC_TOOL_NAME = re.compile(r"^[A-Za-z][A-Za-z_]*$")
_INTERNAL_TOOL_NAMESPACE = re.compile(r"^_[A-Za-z][A-Za-z_]*$")


def encode_tool_name(namespace: str, tool_name: str) -> str:
    """Return one model-facing tool name for one namespaced leaf tool."""

    require_tool_namespace(namespace)
    require_public_tool_name(tool_name, kind="tool")
    return f"{namespace}__{tool_name}"


def join_tool_name(toolset_name: str, tool_name: str) -> str:
    """Return one model-facing tool name for one toolset leaf tool."""

    return encode_tool_name(toolset_name, tool_name)


def require_public_tool_name(value: str, *, kind: str) -> None:
    """Require one public tool identity component."""

    if _PUBLIC_TOOL_NAME.fullmatch(value) and "__" not in value:
        return
    raise ToolangError(
        f"{kind} name must start with an ASCII letter, contain only ASCII "
        f"letters and underscores, and not contain '__': {value!r}"
    )


def require_tool_namespace(value: str) -> None:
    """Require one public or Toolang-internal tool namespace."""

    if "__" not in value and (
        _PUBLIC_TOOL_NAME.fullmatch(value) or _INTERNAL_TOOL_NAMESPACE.fullmatch(value)
    ):
        return
    raise ToolangError(
        "namespace name must be a public name or have exactly one leading "
        f"underscore, and not contain '__': {value!r}"
    )


def is_internal_tool_namespace(value: str) -> bool:
    """Return whether one valid namespace is Toolang-internal."""

    require_tool_namespace(value)
    return value.startswith("_")
