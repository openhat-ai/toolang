"""Workspace URI encoding and explicit directory grants."""

from __future__ import annotations

import os
from pathlib import Path
import re
from urllib.parse import quote, unquote

from ..errors import ToolangError
from ..types.tool import ToolContext

_URI = re.compile(r"workspace://([a-z0-9]+(?:-[a-z0-9]+)*)(/[^?#]*)?")
_BAD_ESCAPE = re.compile(r"%(?![0-9a-fA-F]{2})")


def parse_workspace_uri(value: str) -> tuple[str, str]:
    """Validate an external URI once and decode its workspace-relative path."""

    match = _URI.fullmatch(value)
    if match is None or _BAD_ESCAPE.search(value):
        raise ToolangError(f"invalid workspace URI: {value}")
    try:
        path = unquote(match[2] or "/", errors="strict")
    except UnicodeDecodeError as exc:
        raise ToolangError(f"invalid workspace URI: {value}") from exc
    if "\x00" in path:
        raise ToolangError("workspace URI contains a null byte")
    return match[1], path


def workspace_uri(name: str, path: str = "/") -> str:
    """Encode a known workspace-relative path, not an operating-system path."""

    return f"workspace://{name}/" + quote(path.lstrip("/"), safe="/")


def authorize_workspace_path(path: Path, root: Path) -> Path:
    """Resolve a path without granting access outside its workspace root."""

    resolved = path.resolve()
    if not resolved.is_relative_to(root):
        raise ToolangError("path escapes workspace")
    return resolved


def workspace_root(name: str, context: ToolContext) -> Path:
    """Read one directory grant from the captured Tool Step context."""

    if name not in context.workspaces:
        raise ToolangError(f"workspace is not available: {name}")
    root = context.workspaces[name]
    # The empty path is reserved for the root snapshot; operations use nonempty
    # paths. Resolve only requested roots, not every workspace for every tool.
    key = (root, "", True)
    if key not in context._paths:
        context._paths[key] = (root.resolve(), "/")
    root = context._paths[key][0]
    if not root.is_dir():
        raise ToolangError(f"workspace directory is not available: {name}")
    return root


def resolve_workspace_path(
    name: str, value: str, context: ToolContext, *, follow: bool = True
) -> tuple[Path, str]:
    """Bind a workspace-relative path to a concrete, authorized target."""

    root = workspace_root(name, context)
    key = (root, value, follow)
    if key in context._paths:
        return context._paths[key]
    candidate = root / value.lstrip("/")
    normalized = Path(os.path.abspath(candidate))
    if not normalized.is_relative_to(root):
        raise ToolangError(f"path escapes workspace: {name}")
    resolved = authorize_workspace_path(candidate, root)
    # Preserve a logical alias unless parent traversal changes its meaning.
    if ".." in candidate.parts and normalized.resolve() != resolved:
        normalized = resolved
    relative = "/" + normalized.relative_to(root).as_posix()
    relative = "/" if relative == "/." else relative
    if not follow:
        entry = root / relative.lstrip("/")
        if entry == root:
            raise ToolangError("cannot remove a workspace root")
        # Unlink the addressed entry, not a final symlink's target.
        resolved = authorize_workspace_path(entry.parent, root) / entry.name
    result = (resolved, relative)
    context._paths[key] = result
    return result
